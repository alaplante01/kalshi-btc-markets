#!/usr/bin/env python3
"""Backfill 1-minute candlesticks + trades for recent Kalshi BTC markets into SQLite.

Pulls settled/closed/open markets for the given series over the lookback window,
then per market: 1m candles (bid/ask OHLC + volume + OI) and the public trade tape.

Usage: python backfill.py [--db data/kalshi.db] [--series KXBTC15M] [--hours 48]
"""
import argparse
import calendar
import os
import sqlite3
import time

from kalshi_client import KalshiREST

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
  ticker TEXT, end_ts INTEGER, prev_price REAL,
  bid_o REAL, bid_h REAL, bid_l REAL, bid_c REAL,
  ask_o REAL, ask_h REAL, ask_l REAL, ask_c REAL,
  volume REAL, oi REAL,
  PRIMARY KEY(ticker, end_ts));
CREATE TABLE IF NOT EXISTS trades(
  trade_id TEXT PRIMARY KEY, ticker TEXT, ts_ms INTEGER,
  yes_price REAL, no_price REAL, count REAL,
  taker_side TEXT, taker_outcome_side TEXT, taker_book_side TEXT, is_block INTEGER);
CREATE TABLE IF NOT EXISTS backfill_markets(
  ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, strike_type TEXT,
  floor_strike REAL, cap_strike REAL, open_time TEXT, close_time TEXT,
  status TEXT, result TEXT, volume REAL);
"""

RPS = 3.0
RETRIES = 4


def with_backoff(fn):
    for attempt in range(RETRIES):
        try:
            return fn()
        except Exception as e:
            if "429" not in str(e) or attempt == RETRIES - 1:
                raise
            time.sleep(5 * 3 ** attempt)


def fnum(v):
    return None if v in (None, "") else float(v)


def throttle(last):
    wait = last + 1.0 / RPS - time.time()
    if wait > 0:
        time.sleep(wait)
    return time.time()


def iso_to_ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def fetch_markets(rest, series, since_ts):
    out = []
    for status in ("settled", "closed", "open"):
        out += rest.paged("/trade-api/v2/markets",
                          {"series_ticker": series, "status": status,
                           "min_close_ts": since_ts, "limit": 200},
                          list_key="markets")
    return out


def save_market(db, series, m):
    db.execute("INSERT OR REPLACE INTO backfill_markets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (m["ticker"], m.get("event_ticker"), series, m.get("strike_type"),
                m.get("floor_strike"), m.get("cap_strike"), m.get("open_time"),
                m.get("close_time"), m.get("status"), m.get("result"),
                fnum(m.get("volume_fp"))))


def save_candles(db, ticker, candles):
    for c in candles:
        b, a = c.get("yes_bid", {}), c.get("yes_ask", {})
        db.execute("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ticker, c.get("end_period_ts"),
                    fnum(c.get("price", {}).get("previous_dollars")),
                    fnum(b.get("open_dollars")), fnum(b.get("high_dollars")),
                    fnum(b.get("low_dollars")), fnum(b.get("close_dollars")),
                    fnum(a.get("open_dollars")), fnum(a.get("high_dollars")),
                    fnum(a.get("low_dollars")), fnum(a.get("close_dollars")),
                    fnum(c.get("volume_fp")), fnum(c.get("open_interest_fp"))))


def save_trades(db, trades):
    for t in trades:
        created = t.get("created_time")
        ts_ms = iso_to_ts(created) * 1000 if created else None
        db.execute("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (t["trade_id"], t.get("ticker"), ts_ms,
                    fnum(t.get("yes_price_dollars")), fnum(t.get("no_price_dollars")),
                    fnum(t.get("count_fp")), t.get("taker_side"),
                    t.get("taker_outcome_side"), t.get("taker_book_side"),
                    1 if t.get("is_block_trade") else 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kalshi.db")
    ap.add_argument("--series", default="KXBTC15M")
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--no-trades", action="store_true")
    ap.add_argument("--repair", action="store_true",
                    help="only fetch markets with no candles/trades rows yet")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=15000")  # live collector commits every 0.5s; wait it out
    db.executescript(SCHEMA)
    rest = KalshiREST()
    since = int(time.time() - args.hours * 3600)
    last = 0.0

    for series in args.series.split(","):
        last = throttle(last)
        markets = fetch_markets(rest, series, since)
        print(f"{series}: {len(markets)} markets since {args.hours}h ago", flush=True)
        for i, m in enumerate(markets):
            ticker = m["ticker"]
            save_market(db, series, m)
            db.commit()  # release the write lock before the candle request and any 429 backoff
            start = iso_to_ts(m["open_time"]) - 60 if m.get("open_time") else since
            end = min(int(time.time()),
                      iso_to_ts(m["close_time"]) + 60 if m.get("close_time") else int(time.time()))
            have_candles = args.repair and db.execute(
                "SELECT 1 FROM candles WHERE ticker=? LIMIT 1", (ticker,)).fetchone()
            if not have_candles:
                last = throttle(last)
                try:
                    d = with_backoff(lambda: rest.get(
                        f"/trade-api/v2/series/{series}/markets/{ticker}/candlesticks",
                        {"start_ts": start, "end_ts": end, "period_interval": 1}))
                    save_candles(db, ticker, d.get("candlesticks", []))
                except Exception as e:
                    print(f"  candles fail {ticker}: {e!r}", flush=True)
            db.commit()  # never hold a write txn across the trades network fetch
            have_trades = args.repair and db.execute(
                "SELECT 1 FROM trades WHERE ticker=? LIMIT 1", (ticker,)).fetchone()
            if not args.no_trades and fnum(m.get("volume_fp")) and not have_trades:
                last = throttle(last)
                try:
                    trades = with_backoff(lambda: rest.paged(
                        "/trade-api/v2/markets/trades", {"ticker": ticker, "limit": 1000},
                        list_key="trades", limit_pages=80))
                    save_trades(db, trades)
                except Exception as e:
                    print(f"  trades fail {ticker}: {e!r}", flush=True)
            db.commit()  # per market: keep write-lock hold times short for the live collector
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(markets)}", flush=True)
        db.commit()

    for table in ("backfill_markets", "candles", "trades"):
        n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {n} rows", flush=True)


if __name__ == "__main__":
    main()
