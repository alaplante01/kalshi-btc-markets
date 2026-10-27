#!/usr/bin/env python3
"""Kalshi BTC market-data collector.

Two websocket connections:
  A (stable):  cfbenchmarks_value + cfbenchmarks_value_5hz for BRTI — the settlement
               index, incl. the running settlement average during the final minute
               of each quarter-hour.
  B (rebuilt): ticker + trade on every market of the tracked series' open events,
               orderbook_delta on the actively-quoted band. Rebuilt whenever REST
               discovery (every 30s) changes the wanted set.

Single-writer design: feed handlers only enqueue; one writer task owns SQLite and
batches commits. Raw book messages are stored verbatim for replay.

Usage: python collector.py [--db data/kalshi.db] [--series KXBTC15M,KXBTC,KXBTCD]
"""
import argparse
import asyncio
import json
import os
import sqlite3
import time

import websockets

from kalshi_client import WS_URL, KalshiAuth, KalshiREST

SCHEMA = """
CREATE TABLE IF NOT EXISTS brti(
  received_at_ms INTEGER, src_ts_ms INTEGER, value REAL,
  avg60 REAL, avg60_n INTEGER, settle_avg REAL, settle_n INTEGER, src TEXT);
CREATE TABLE IF NOT EXISTS trades(
  trade_id TEXT PRIMARY KEY, ticker TEXT, ts_ms INTEGER,
  yes_price REAL, no_price REAL, count REAL,
  taker_side TEXT, taker_outcome_side TEXT, taker_book_side TEXT, is_block INTEGER);
CREATE TABLE IF NOT EXISTS ticker(
  ts_ms INTEGER, ticker TEXT, price REAL, yes_bid REAL, yes_ask REAL,
  volume REAL, oi REAL, yes_bid_size REAL, yes_ask_size REAL, last_size REAL);
CREATE TABLE IF NOT EXISTS book(
  local_ms INTEGER, ticker TEXT, kind TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS markets(
  fetched_ms INTEGER, ticker TEXT, event_ticker TEXT, series TEXT,
  strike_type TEXT, floor_strike REAL, cap_strike REAL,
  open_time TEXT, close_time TEXT, status TEXT,
  PRIMARY KEY(ticker, fetched_ms));
CREATE INDEX IF NOT EXISTS idx_brti_ts ON brti(src_ts_ms);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_ticker_t ON ticker(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_book_t ON book(ticker, local_ms);
"""

DISCOVERY_INTERVAL = 30
BOOK_BAND = (0.02, 0.98)   # subscribe orderbook only where it's actually quoted
BOOK_CAP = 40
COMMIT_EVERY_ROWS = 200
COMMIT_EVERY_SECS = 0.5

SQL = {
    "brti": "INSERT INTO brti VALUES(?,?,?,?,?,?,?,?)",
    "trade": "INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)",
    "ticker": "INSERT INTO ticker VALUES(?,?,?,?,?,?,?,?,?,?)",
    "book": "INSERT INTO book VALUES(?,?,?,?)",
    "market": "INSERT OR IGNORE INTO markets VALUES(?,?,?,?,?,?,?,?,?,?)",
}


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def now_ms():
    return int(time.time() * 1000)


def fnum(v):
    return None if v in (None, "") else float(v)


def brti_row(m, src):
    raw = json.loads(m.get("data", "{}"))
    avg = m.get("avg_60s_data") or {}
    settle = m.get("last_60s_windowed_average_15min") or {}
    return (m.get("received_at"), raw.get("time"), fnum(raw.get("value")),
            fnum(avg.get("value")), avg.get("window_size"),
            fnum(settle.get("value")), settle.get("window_size"), src)


def trade_row(m):
    return (m.get("trade_id"), m.get("market_ticker"), m.get("ts_ms"),
            fnum(m.get("yes_price_dollars")), fnum(m.get("no_price_dollars")),
            fnum(m.get("count_fp")), m.get("taker_side"),
            m.get("taker_outcome_side"), m.get("taker_book_side"),
            1 if m.get("is_block_trade") else 0)


def ticker_row(m):
    return (m.get("ts_ms"), m.get("market_ticker"), fnum(m.get("price_dollars")),
            fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars")),
            fnum(m.get("volume_fp")), fnum(m.get("open_interest_fp")),
            fnum(m.get("yes_bid_size_fp")), fnum(m.get("yes_ask_size_fp")),
            fnum(m.get("last_trade_size_fp")))


def market_row(fetched_ms, series, m):
    return (fetched_ms, m["ticker"], m.get("event_ticker"), series,
            m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"),
            m.get("open_time"), m.get("close_time"), m.get("status"))


async def writer(db_path, queue):
    """Sole owner of the SQLite connection. Batches commits."""
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    pending, last_commit = 0, time.monotonic()
    while True:
        try:
            kind, row = await asyncio.wait_for(queue.get(), timeout=COMMIT_EVERY_SECS)
        except TimeoutError:
            kind = None
        while kind:
            try:
                db.execute(SQL[kind], row)
                pending += 1
                kind = None
            except sqlite3.OperationalError as e:  # another writer holds the lock
                log(f"writer busy ({e}); retrying")
                await asyncio.sleep(0.2)
        if pending and (pending >= COMMIT_EVERY_ROWS
                        or time.monotonic() - last_commit >= COMMIT_EVERY_SECS):
            try:
                db.commit()
                pending, last_commit = 0, time.monotonic()
            except sqlite3.OperationalError:
                await asyncio.sleep(1)


async def ws_connect(auth):
    return await websockets.connect(WS_URL, additional_headers=auth.ws_headers(),
                                    ping_interval=10, ping_timeout=10)


async def brti_loop(auth, queue):
    backoff = 1
    while True:
        try:
            async with await ws_connect(auth) as ws:
                await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                    "channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]}}))
                await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {
                    "channels": ["cfbenchmarks_value_5hz"], "index_ids": ["BRTI"]}}))
                log("BRTI feed connected (1s + 5hz)")
                backoff = 1
                async for raw in ws:
                    m = json.loads(raw)
                    t = m.get("type")
                    if t == "cfbenchmarks_value":
                        queue.put_nowait(("brti", brti_row(m["msg"], "1s")))
                    elif t == "cfbenchmarks_value_5hz":
                        queue.put_nowait(("brti", brti_row(m["msg"], "5hz")))
                    elif t == "error":
                        log("BRTI ws error:", json.dumps(m)[:200])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"BRTI feed error: {e!r}; reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def market_loop(auth, queue, all_tickers, book_tickers, rebuild_evt):
    backoff = 1
    seqs = {}
    while not rebuild_evt.is_set():
        try:
            async with await ws_connect(auth) as ws:
                await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                    "channels": ["ticker", "trade"], "market_tickers": all_tickers}}))
                if book_tickers:
                    await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta"], "market_tickers": book_tickers}}))
                log(f"market feed connected: {len(all_tickers)} tickers, "
                    f"{len(book_tickers)} books")
                backoff = 1
                seqs.clear()
                while not rebuild_evt.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except TimeoutError:
                        continue  # idle is normal off-hours; keepalive pings cover us
                    m = json.loads(raw)
                    t = m.get("type")
                    if t == "trade":
                        queue.put_nowait(("trade", trade_row(m["msg"])))
                    elif t == "ticker":
                        queue.put_nowait(("ticker", ticker_row(m["msg"])))
                    elif t in ("orderbook_snapshot", "orderbook_delta"):
                        sid, seq = m.get("sid"), m.get("seq")
                        if seq is not None and sid in seqs and seq != seqs[sid] + 1:
                            log(f"book seq gap sid={sid}: {seqs[sid]} -> {seq}")
                        seqs[sid] = seq if seq is not None else seqs.get(sid)
                        queue.put_nowait(("book",
                                          (now_ms(), m["msg"].get("market_ticker"),
                                           t, json.dumps(m["msg"]))))
                    elif t == "error":
                        log("market ws error:", json.dumps(m)[:200])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if rebuild_evt.is_set():
                return
            log(f"market feed error: {e!r}; reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def discover(rest, series_list):
    """Pure REST. Returns (all_tickers, book_tickers, market_rows)."""
    all_tickers, book_tickers, rows = [], [], []
    fetched = now_ms()
    for series in series_list:
        events = rest.paged("/trade-api/v2/events",
                            {"series_ticker": series, "status": "open",
                             "with_nested_markets": "true", "limit": 20},
                            list_key="events")
        for e in events:
            for m in e.get("markets", []):
                if m.get("status") != "active":
                    continue
                rows.append(market_row(fetched, series, m))
                all_tickers.append(m["ticker"])
                bid = fnum(m.get("yes_bid_dollars")) or 0
                ask = fnum(m.get("yes_ask_dollars"))
                quoted = bid >= BOOK_BAND[0] or (ask is not None and ask <= BOOK_BAND[1])
                if series.endswith("15M") or quoted:
                    book_tickers.append(m["ticker"])
    # The 15-minute contracts are what the terminal prices, so they take book slots
    # first; the alphabetical order alone put every KXBTC-... ticker ahead of KXBTC15M-...
    first = sorted({t for t in book_tickers if "15M-" in t})
    rest = sorted(set(book_tickers) - set(first))
    return sorted(set(all_tickers)), (first + rest)[:BOOK_CAP], rows


async def supervisor(auth, rest, queue, series_list):
    current = (None, None)
    task, rebuild_evt = None, None
    while True:
        try:
            all_t, book_t, rows = await asyncio.to_thread(discover, rest, series_list)
            for r in rows:
                queue.put_nowait(("market", r))
        except Exception as e:
            log(f"discovery error: {e!r}")
            await asyncio.sleep(DISCOVERY_INTERVAL)
            continue
        if (all_t, book_t) != current and all_t:
            if task:
                rebuild_evt.set()
                task.cancel()
            rebuild_evt = asyncio.Event()
            task = asyncio.create_task(
                market_loop(auth, queue, all_t, book_t, rebuild_evt))
            current = (all_t, book_t)
            log(f"discovery: {len(all_t)} markets tracked, {len(book_t)} books")
        await asyncio.sleep(DISCOVERY_INTERVAL)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kalshi.db")
    ap.add_argument("--series", default="KXBTC15M,KXBTC,KXBTCD")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    auth = KalshiAuth()
    rest = KalshiREST()
    queue = asyncio.Queue(maxsize=100_000)
    series_list = args.series.split(",")
    log(f"collector starting: {series_list} -> {args.db}")
    await asyncio.gather(writer(args.db, queue),
                         brti_loop(auth, queue),
                         supervisor(auth, rest, queue, series_list))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("collector stopped")
