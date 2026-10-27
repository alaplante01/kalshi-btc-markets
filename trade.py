#!/usr/bin/env python3
"""Kalshi execution CLI. Every write is a dry run unless --send is given.

    python trade.py balance
    python trade.py quote KXBTC15M-26SEP300100-00
    python trade.py orders | positions
    python trade.py group --limit 400          # create rolling-15s kill-switch group (saved, reused)
    python trade.py yes  TICKER --count 50 --at 0.55        # maker bid for YES at 55c (post_only GTC)
    python trade.py no   TICKER --count 50 --at 0.57        # maker bid for NO at 57c (= ask YES @0.43)
    python trade.py yes  TICKER --count 50 --at 0.55 --taker  # cross the book (IOC, pays taker fee)
    python trade.py cancel ORDER_ID --market TICKER
    python trade.py cancel-all

Every order: exchange_index=2 (crypto shard), attached to the saved order group when one
exists, count capped at MAX_CONTRACTS without --force. Default is a dry run that prints
the exact payload; add --send to transmit. Never logs key material.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from kalshi_client import CRED_DIR, REST_BASE, KalshiAuth

MAX_CONTRACTS = 200
NET_POSITION_CAP = 300        # max absolute net contracts per market after the order
DAILY_STOP = 50.0             # hard brake: no new orders once realized 24h P&L is below -$50
SHARD = 2
GROUP_FILE = Path(CRED_DIR) / "order-group.json"


def call(auth, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(REST_BASE + path, data=data, method=method,
                                 headers={**auth.headers(method, path),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read()[:800].decode(errors='replace')}")


def get(auth, path):
    return call(auth, "GET", path)


def group_id():
    if GROUP_FILE.exists():
        return json.loads(GROUP_FILE.read_text()).get("order_group_id")
    return None


def cmd_balance(auth, _):
    b = get(auth, "/trade-api/v2/portfolio/balance")
    print(f"total ${b.get('balance_dollars')}")
    for s in b.get("balance_breakdown", []):
        print(f"  shard {s['exchange_index']}: ${s['balance']}"
              + ("  <- crypto" if s["exchange_index"] == SHARD else ""))


def cmd_quote(auth, a):
    d = get(auth, f"/trade-api/v2/markets/{a.ticker}/orderbook?depth=3")
    ob = d.get("orderbook_fp", {})
    yes, no = ob.get("yes_dollars") or [], ob.get("no_dollars") or []
    best_bid = yes[-1] if yes else None
    best_no = no[-1] if no else None
    ask = round(1 - float(best_no[0]), 2) if best_no else None
    print(f"{a.ticker}\n  yes bid {best_bid[0] if best_bid else '--'} x {best_bid[1] if best_bid else ''}"
          f"\n  yes ask {ask} (no bid {best_no[0] if best_no else '--'} x {best_no[1] if best_no else ''})")


def cmd_orders(auth, _):
    for o in get(auth, "/trade-api/v2/portfolio/orders?status=resting").get("orders", []):
        print(f"{o.get('order_id')}  {o.get('ticker')}  {o.get('side')} "
              f"{o.get('remaining_count') or o.get('count_fp')} @ {o.get('price_dollars') or o.get('yes_price_dollars')}")


def cmd_positions(auth, _):
    for p in get(auth, "/trade-api/v2/portfolio/positions?count_filter=position").get("market_positions", []):
        print(f"{p.get('ticker')}  pos {p.get('position_fp') or p.get('position')}  "
              f"exposure ${p.get('market_exposure_dollars')}")


def cmd_group(auth, a):
    if group_id():
        print(f"group exists: {group_id()} ({GROUP_FILE})")
        return
    body = {"contracts_limit_fp": f"{a.limit:.2f}", "exchange_index": SHARD}
    if not a.send:
        print(f"DRY RUN — would create order group {body}. Add --send.")
        return
    GROUP_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    res = call(auth, "POST", "/trade-api/v2/portfolio/order_groups/create", body)
    gid = res.get("order_group_id") or res.get("order_group", {}).get("order_group_id")
    GROUP_FILE.write_text(json.dumps({"order_group_id": gid, "limit": a.limit}))
    print(f"order group {gid} (rolling-15s limit {a.limit} contracts) -> {GROUP_FILE}")


def fill_side(f):
    """Which outcome a Fill left us long. Kalshi reports it in `outcome_side`; the legacy
    `side` names the side the order was placed on, so side=yes with action=sell means NO."""
    s = f.get("outcome_side")
    if s:
        return s
    side = f.get("side")
    if f.get("action", "buy") == "buy":
        return side
    return {"yes": "no", "no": "yes"}.get(side)


def pnl_24h(auth):
    """Realized P&L (incl. taker fees) on markets settled in the last 24h."""
    import time as _time
    now = _time.time()
    day_ago = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    two_days = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 172800))
    setts = {s["ticker"]: s.get("market_result")
             for s in get(auth, "/trade-api/v2/portfolio/settlements?limit=200").get("settlements", [])
             if s.get("settled_time", "") >= day_ago}
    fills, cur = [], None
    for _ in range(10):
        path = "/trade-api/v2/portfolio/fills?limit=200" + (f"&cursor={cur}" if cur else "")
        d = get(auth, path)
        page = d.get("fills", [])
        fills += [f for f in page if f.get("ticker") in setts]
        cur = d.get("cursor")
        if not cur or (page and page[-1].get("created_time", "") < two_days):
            break
    pnl = {}
    for f in fills:
        t, s = f["ticker"], fill_side(f)
        c = float(f.get("count_fp") or 0)
        p = float(f.get("yes_price_dollars") or 0)
        m = pnl.setdefault(t, {"yes": 0.0, "no": 0.0, "out": 0.0})
        m[s] += c
        m["out"] += c * (p if s == "yes" else 1 - p)
        m["out"] += 0.07 * c * p * (1 - p) if f.get("is_taker", True) else 0
    return sum(m[setts[t]] - m["out"] for t, m in pnl.items() if setts.get(t) in ("yes", "no"))


def net_position(auth, ticker):
    for p in get(auth, f"/trade-api/v2/portfolio/positions?ticker={ticker}").get("market_positions", []):
        if p.get("ticker") == ticker:
            return float(p.get("position_fp") or p.get("position") or 0)
    return 0.0


def order(auth, a, outcome):
    if a.count > MAX_CONTRACTS and not a.force:
        sys.exit(f"count {a.count} > cap {MAX_CONTRACTS} — add --force if you mean it")
    if a.send:
        pos = net_position(auth, a.ticker)
        delta = a.count if outcome == "yes" else -a.count
        reduces = abs(pos + delta) < abs(pos)  # exits are never blocked by the loss brake
        if not reduces:
            realized = pnl_24h(auth)
            if realized <= -DAILY_STOP:
                sys.exit(f"BRAKE: realized 24h P&L is ${realized:.2f} (stop is -${DAILY_STOP:.0f}). "
                         f"No new orders until the 24h window clears. (Exits still allowed.)")
            print(f"brakes ok: 24h P&L ${realized:.2f}", end=", ")
        if abs(pos + delta) > NET_POSITION_CAP:
            sys.exit(f"BRAKE: net position would be {pos + delta:+.0f} contracts "
                     f"(cap {NET_POSITION_CAP}). Current: {pos:+.0f}.")
        print(f"position {pos:+.0f} -> {pos + delta:+.0f}" + ("  [reducing]" if reduces else ""))
    if outcome == "yes":
        side, price, shown = "bid", a.at, f"YES @{a.at:.2f}"
    else:
        side, price, shown = "ask", round(1 - a.at, 2), f"NO @{a.at:.2f} (= YES ask @{round(1 - a.at, 2):.2f})"
    body = {"ticker": a.ticker, "side": side, "count": f"{a.count:.2f}",
            "price": f"{price:.4f}", "client_order_id": str(uuid.uuid4()),
            "time_in_force": "immediate_or_cancel" if a.taker else "good_till_canceled",
            "post_only": not a.taker, "self_trade_prevention_type": "taker_at_cross",
            "exchange_index": SHARD}
    if group_id():
        body["order_group_id"] = group_id()
    print(f"{'ORDER' if a.send else 'DRY RUN'}: {a.count} {shown} on {a.ticker} "
          f"[{'taker IOC' if a.taker else 'maker post-only GTC'}]"
          + (f" group {group_id()}" if group_id() else "  (no order group — run `group` first)"))
    print(json.dumps(body, indent=1))
    if not a.send:
        print("nothing sent — add --send to transmit")
        return
    res = call(auth, "POST", "/trade-api/v2/portfolio/events/orders", body)
    print(json.dumps(res, indent=1)[:800])


def cmd_fills(auth, _):
    for f in get(auth, "/trade-api/v2/portfolio/fills?limit=200").get("fills", []):
        print(f"{f.get('created_time','')[:19]}Z {f.get('ticker','')}  "
              f"{f.get('taker_outcome_side') or f.get('side','')} "
              f"{f.get('count_fp') or f.get('count')} @ yes {f.get('yes_price_dollars')} "
              f"{'taker' if f.get('is_taker') else 'maker'}")


def cmd_settlements(auth, _):
    for s in get(auth, "/trade-api/v2/portfolio/settlements?limit=100").get("settlements", []):
        print(f"{s.get('settled_time','')[:19]}Z {s.get('ticker','')}  result={s.get('market_result')} "
              f"yes={s.get('yes_count_fp') or s.get('yes_count')} no={s.get('no_count_fp') or s.get('no_count')} "
              f"revenue=${s.get('revenue_dollars', s.get('revenue'))}")


def cmd_analyze(auth, _):
    """Walk full fill history + settlements; print per-market P&L, fees, churn."""
    fills, cur = [], None
    for _ in range(80):
        path = "/trade-api/v2/portfolio/fills?limit=200" + (f"&cursor={cur}" if cur else "")
        d = get(auth, path)
        fills += d.get("fills", [])
        cur = d.get("cursor")
        if not cur:
            break
    setts, cur = {}, None
    for _ in range(40):
        path = "/trade-api/v2/portfolio/settlements?limit=200" + (f"&cursor={cur}" if cur else "")
        d = get(auth, path)
        for s in d.get("settlements", []):
            setts[s["ticker"]] = s.get("market_result")
        cur = d.get("cursor")
        if not cur:
            break

    mkts = {}
    for f in fills:
        t = f.get("ticker")
        s = fill_side(f)
        c = float(f.get("count_fp") or f.get("count") or 0)
        p = float(f.get("yes_price_dollars") or 0)
        m = mkts.setdefault(t, {"yes": 0.0, "no": 0.0, "cost": 0.0, "fees": 0.0,
                                "nfills": 0, "flips": 0, "last": None,
                                "day": f.get("created_time", "")[:10]})
        m[s] = m.get(s, 0) + c
        m["cost"] += c * (p if s == "yes" else 1 - p)
        if f.get("is_taker", True):
            m["fees"] += 0.07 * c * p * (1 - p)
        m["nfills"] += 1
        if m["last"] and m["last"] != s:
            m["flips"] += 1
        m["last"] = s

    rows, days = [], {}
    for t, m in mkts.items():
        r = setts.get(t)
        credit = m["yes"] if r == "yes" else m["no"] if r == "no" else None
        pnl = (credit - m["cost"] - m["fees"]) if credit is not None else None
        rows.append((t, m, r, pnl))
        if pnl is not None:
            d = days.setdefault(m["day"], [0.0, 0.0, 0.0])
            d[0] += pnl; d[1] += m["fees"]; d[2] += m["yes"] + m["no"]
    rows.sort(key=lambda x: (x[3] is None, x[3] if x[3] is not None else 0))

    print(f"{len(fills)} fills across {len(mkts)} markets "
          f"({sum(1 for f in fills if not f.get('is_taker', True))} maker fills)")
    print("\nWORST 12 MARKETS:")
    for t, m, r, pnl in [x for x in rows if x[3] is not None][:12]:
        flat = " FLAT" if abs(m["yes"] - m["no"]) < 1 else ""
        print(f"  {t:<34} pnl ${pnl:>9.2f}  fees ${m['fees']:>7.2f}  "
              f"yes {m['yes']:.0f}/no {m['no']:.0f}{flat}  {m['nfills']}f/{m['flips']}flips  -> {r}")
    print("\nBEST 5:")
    for t, m, r, pnl in [x for x in rows if x[3] is not None][-5:]:
        print(f"  {t:<34} pnl ${pnl:>9.2f}  fees ${m['fees']:>7.2f}  -> {r}")
    print("\nPER DAY (UTC):")
    tot_p = tot_f = 0.0
    for d in sorted(days):
        p, f, v = days[d]
        tot_p += p; tot_f += f
        print(f"  {d}  pnl ${p:>9.2f}   fees ${f:>8.2f}   contracts {v:>10.0f}")
    flat_n = sum(1 for _, m, r, pnl in rows if pnl is not None and abs(m["yes"] - m["no"]) < 1 and m["yes"] > 0)
    print(f"\nTOTAL settled pnl ${tot_p:.2f}   fees ${tot_f:.2f}   "
          f"flat-at-settle markets: {flat_n}   open/unsettled markets: {sum(1 for x in rows if x[3] is None)}")


def cmd_cancel(auth, a):
    path = f"/trade-api/v2/portfolio/events/orders/{a.order_id}?market_ticker={a.market}"
    if not a.send:
        print(f"DRY RUN — would DELETE {path}. Add --send.")
        return
    print(json.dumps(call(auth, "DELETE", path), indent=1))


def cmd_cancel_all(auth, a):
    if not a.send:
        print("DRY RUN — would cancel every resting order (all markets). Add --send.")
        return
    print(json.dumps(call(auth, "DELETE", "/trade-api/v2/portfolio/events/orders"), indent=1)[:800])


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("balance")
    q = sub.add_parser("quote"); q.add_argument("ticker")
    sub.add_parser("orders"); sub.add_parser("positions")
    sub.add_parser("fills"); sub.add_parser("settlements"); sub.add_parser("analyze")
    g = sub.add_parser("group"); g.add_argument("--limit", type=int, default=400); g.add_argument("--send", action="store_true")
    for name in ("yes", "no"):
        o = sub.add_parser(name)
        o.add_argument("ticker"); o.add_argument("--count", type=int, required=True)
        o.add_argument("--at", type=float, required=True, help="price of the OUTCOME you're buying, in dollars")
        o.add_argument("--taker", action="store_true"); o.add_argument("--send", action="store_true")
        o.add_argument("--force", action="store_true")
    c = sub.add_parser("cancel"); c.add_argument("order_id"); c.add_argument("--market", required=True); c.add_argument("--send", action="store_true")
    ca = sub.add_parser("cancel-all"); ca.add_argument("--send", action="store_true")
    a = ap.parse_args()
    auth = KalshiAuth()
    {"balance": cmd_balance, "quote": cmd_quote, "orders": cmd_orders, "positions": cmd_positions,
     "fills": cmd_fills, "settlements": cmd_settlements, "analyze": cmd_analyze,
     "group": cmd_group, "cancel": cmd_cancel, "cancel-all": cmd_cancel_all,
     "yes": lambda au, ar: order(au, ar, "yes"), "no": lambda au, ar: order(au, ar, "no")}[a.cmd](auth, a)


if __name__ == "__main__":
    main()
