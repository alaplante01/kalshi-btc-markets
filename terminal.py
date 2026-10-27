#!/usr/bin/env python3
"""Live KXBTC15M signal terminal — clean full-screen readout off the collector DB.

The collector must be running (it feeds the DB). Rings the bell + logs to
signals.log when a signal fires.

  TAKE YES / TAKE NO  model probability beats the ask by fee + edge buffer
  LOCKED UP / DOWN    final minute: flipping settlement needs a >3-sigma move
                      but the market still prices under 0.94

Usage: python terminal.py [--db data/kalshi.db] [--edge 0.08]
"""
import argparse
import calendar
import json
import math
import sqlite3
import sys
import time
import urllib.request

from kalshi_client import REST_BASE

G, R, Y, D, B, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
HOME, CLR, HIDE, SHOW = "\033[H", "\033[K", "\033[?25l", "\033[?25h"
SPARK = "▁▂▃▄▅▆▇█"
LOCK_MAX_PRICE = 0.94
VOL_WINDOW_S = 1200
FLOW_WINDOW_S = 300
REST = f"{REST_BASE}/trade-api/v2"


def taker_fee(p):
    return 0.07 * p * (1 - p)


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def utc(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


class Feed:
    def __init__(self, db_path):
        self.db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.db.execute("PRAGMA busy_timeout=2000")

    def brti(self):
        rows = self.db.execute(
            "SELECT src_ts_ms, value, settle_avg, settle_n FROM brti "
            "WHERE src='1s' AND value IS NOT NULL ORDER BY src_ts_ms DESC LIMIT 5").fetchall()
        if not rows:
            return None
        ts, v, sa, sn = rows[0]
        return ts, v, sa, sn, sum(r[1] for r in rows) / len(rows)

    def sigma_1s(self):
        rows = self.db.execute(
            "SELECT value FROM brti WHERE src='1s' AND value IS NOT NULL "
            "AND src_ts_ms > ? ORDER BY src_ts_ms",
            ((time.time() - VOL_WINDOW_S) * 1000,)).fetchall()
        d = [rows[i][0] - rows[i - 1][0] for i in range(1, len(rows))]
        if len(d) < 30:
            return None
        m = sum(d) / len(d)
        return math.sqrt(sum((x - m) ** 2 for x in d) / len(d))

    def market(self):
        return self.db.execute(
            "SELECT ticker, floor_strike, close_time FROM markets "
            "WHERE series='KXBTC15M' AND status='active' "
            "AND close_time > strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "ORDER BY fetched_ms DESC, close_time ASC LIMIT 1").fetchone()

    def quote(self, ticker):
        return self.db.execute(
            "SELECT yes_bid, yes_ask FROM ticker WHERE ticker=? "
            "ORDER BY ts_ms DESC LIMIT 1", (ticker,)).fetchone()

    def flow(self):
        r = self.db.execute(
            "SELECT SUM(CASE WHEN taker_outcome_side='yes' THEN count ELSE 0 END),"
            "       SUM(CASE WHEN taker_outcome_side='no' THEN count ELSE 0 END) "
            "FROM trades WHERE ticker LIKE 'KXBTC15M%' AND ts_ms > ?",
            ((time.time() - FLOW_WINDOW_S) * 1000,)).fetchone()
        return r[0] or 0, r[1] or 0

    def spark(self, minutes=15, points=40):
        step = minutes * 60 // points
        rows = self.db.execute(
            "SELECT (src_ts_ms/1000)/? , AVG(value) FROM brti "
            "WHERE src='1s' AND value IS NOT NULL AND src_ts_ms > ? "
            "GROUP BY 1 ORDER BY 1", (step, (time.time() - minutes * 60) * 1000)).fetchall()
        vals = [r[1] for r in rows][-points:]
        if len(vals) < 2:
            return "", None, None
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1
        return "".join(SPARK[int((v - lo) / rng * 7)] for v in vals), lo, hi


def recent_results(cache={}):
    if time.time() - cache.get("t", 0) > 300:
        try:
            u = (f"{REST}/markets?series_ticker=KXBTC15M&status=settled"
                 f"&min_close_ts={int(time.time()) - 7500}&limit=10")
            ms = json.loads(urllib.request.urlopen(u, timeout=10).read()).get("markets", [])
            ms.sort(key=lambda m: m["close_time"])
            cache["v"] = [(m["close_time"][11:16], m.get("result")) for m in ms][-8:]
            cache["t"] = time.time()
        except Exception:
            cache.setdefault("v", [])
            cache["t"] = time.time() - 240
    return cache.get("v", [])


def model_p(value, strike, sigma, t_left, sa, sn):
    if sn and sa is not None and sn > 0:
        r = 60 - sn
        if r <= 0:
            return (1.0 if sa >= strike else 0.0), 0.0
        proj = (sa * sn + value * r) / 60
        sd = (sigma or 1e-9) * math.sqrt(r ** 3 / 3) / 60
        return phi((proj - strike) / max(sd, 1e-9)), sd
    sd = (sigma or 0) * math.sqrt(max(t_left - 30, 1))
    return phi((value - strike) / max(sd, 1e-9)), sd


def build_signal(p, sd, bid, ask, edge, sn, t_left):
    no_ask = 1 - bid if bid is not None else None
    if sn and sn > 0 and sd > 0:
        if p > 0.999 and ask is not None and ask <= LOCK_MAX_PRICE:
            return f"LOCKED UP — TAKE YES @{ask:.2f}", True
        if p < 0.001 and no_ask is not None and no_ask <= LOCK_MAX_PRICE:
            return f"LOCKED DOWN — TAKE NO @{no_ask:.2f}", True
    if t_left < 120:  # spot check 9/29: Gaussian-vs-market divergence inside 2 min
        return "HOLD", False  # was wrong 8/11 at the 1-min horizon — LOCKED only here
    if ask is not None and p - ask >= taker_fee(ask) + edge:
        return f"TAKE YES @{ask:.2f}  (model {p:.2f})", True
    if no_ask is not None and (1 - p) - no_ask >= taker_fee(no_ask) + edge:
        return f"TAKE NO @{no_ask:.2f}  (model {1 - p:.2f})", True
    return "HOLD", False


def bar(frac, width=24):
    n = max(0, min(width, int(frac * width)))
    return "█" * n + "░" * (width - n)


def render(f, edge, fired_log):
    b = f.brti()
    mkt = f.market()
    out = [HOME]
    w = lambda s="": out.append(s + CLR + "\n")
    w()
    if not b or not mkt:
        w(f"  {Y}waiting for data — is the collector running?{RST}")
        sys.stdout.write("".join(out) + "\033[J")
        return None, False
    ts, value, sa, sn, smooth = b
    ticker, strike, close_time = mkt
    t_left = utc(close_time) - time.time()
    sigma = getattr(render, "sig", None)
    if time.time() - getattr(render, "sig_t", 0) > 30:
        render.sig = sigma = f.sigma_1s() or sigma
        render.sig_t = time.time()
    q = f.quote(ticker)
    bid, ask = (q or (None, None))
    p, sd = model_p(smooth, strike, sigma, t_left, sa, sn)
    up, dn = f.flow()
    tilt = (up - dn) / (up + dn) if up + dn else 0
    sig, fire = build_signal(p, sd, bid, ask, edge, sn, t_left)
    cush = value - strike
    cc = G if cush >= 0 else R
    stale = time.time() - ts / 1000
    arrow = f"{G}▲{RST}" if cush >= 0 else f"{R}▼{RST}"
    spark, lo, hi = f.spark()

    w(f"  {B}BTC 15M{RST}   {D}{ticker}{RST}   closes {close_time[11:16]}Z"
      + (f"   {R}{B}FEED STALE {stale:.0f}s{RST}" if stale > 5 else ""))
    w()
    w(f"  {B}BRTI  {value:>10,.1f}{RST}   {arrow} {cc}{cush:+8.1f}{RST}   vs strike {strike:,.1f}")
    w()
    mins, secs = int(max(t_left, 0)) // 60, int(max(t_left, 0)) % 60
    w(f"  {D}left{RST}  {mins}:{secs:02d}   {bar(1 - t_left / 900)}")
    if sn and sn > 0:
        req = (strike * 60 - sa * sn) / max(60 - sn, 1)
        side = "flip DOWN" if sa * sn / max(sn, 1) >= strike or value >= strike else "flip UP"
        w(f"  {D}settle{RST} {B}{sa:,.1f}{RST}  ({sn}/60)   "
          f"{D}remaining ticks must avg {req:,.1f} to {side}{RST}")
    else:
        w(f"  {D}settle window opens at :{(utc(close_time) % 3600) // 60 - 1:02d}:00{RST}")
    w()
    qs = f"{bid:.2f} / {ask:.2f}" if q else "-- / --"
    w(f"  {D}model{RST} {B}{p:.2f}{RST}   {D}market{RST} {qs}   "
      f"{D}flow 5m{RST} {(G if tilt >= 0 else R)}{tilt:+.0%}{RST}   {D}vol{RST} {sigma or 0:.1f}/s")
    w()
    if spark:
        w(f"  {D}15m{RST}  {spark}  {D}{lo:,.0f}–{hi:,.0f}{RST}")
    res = recent_results()
    if res:
        marks = "  ".join((G + "▲" if r == "yes" else R + "▼") + RST for _, r in res)
        w(f"  {D}last {len(res)}{RST}  {marks}")
    w()
    w("  " + D + "─" * 56 + RST)
    w(f"  {(G + B) if fire else D}{sig}{RST}")
    for line in fired_log[-3:]:
        w(f"  {D}{line}{RST}")
    sys.stdout.write("".join(out) + "\033[J")
    sys.stdout.flush()
    return (sig, ticker, value, strike, p, bid, ask), fire


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kalshi.db")
    ap.add_argument("--edge", type=float, default=0.08)
    args = ap.parse_args()
    f = Feed(args.db)
    last_sig, fired_log = "", []
    sys.stdout.write(HIDE + "\033[2J")
    try:
        while True:
            info, fire = render(f, args.edge, fired_log)
            if info and fire and info[0] != last_sig:
                sig, ticker, value, strike, p, bid, ask = info
                stamp = time.strftime("%H:%M:%S")
                fired_log.append(f"{stamp}  {sig}")
                sys.stdout.write("\a")
                with open("signals.log", "a") as fh:
                    fh.write(f"{stamp} {sig} {ticker} brti={value:.2f} "
                             f"K={strike:.2f} p={p:.3f} bid={bid} ask={ask}\n")
            last_sig = info[0] if (info and fire) else ""
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW + RST + "\n")


if __name__ == "__main__":
    main()
