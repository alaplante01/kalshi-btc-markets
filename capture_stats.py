#!/usr/bin/env python3
"""Summarise a collector database: rows per table, live vs backfilled trades,
BRTI coverage and gaps, and coverage by series.

Opens the file with immutable=1, so it never writes to the database, its WAL or
its shared-memory file. Rows still sitting in an uncheckpointed WAL are not counted.

Usage: python capture_stats.py [data/kalshi.db]
"""
import argparse
import datetime as dt
import sqlite3

GAP_MS = 60_000  # a stretch longer than this without a 1-second BRTI value is a gap
SERIES = "substr(ticker, 1, instr(ticker, '-') - 1)"
# The live feed stores millisecond trade timestamps; backfill.py stores whole seconds.
LIVE_TRADE = "ts_ms % 1000 <> 0"


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def joined(counts):
    return ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items()))


def brti_summary(db):
    lo, hi = db.execute("SELECT MIN(src_ts_ms), MAX(src_ts_ms) FROM brti").fetchone()
    if lo is None:
        return 0
    by_src = dict(db.execute("SELECT src, COUNT(*) FROM brti GROUP BY src"))
    minutes = db.execute(
        "SELECT COUNT(DISTINCT src_ts_ms / 60000) FROM brti WHERE src='1s'").fetchone()[0]
    ts = [r[0] for r in db.execute("SELECT src_ts_ms FROM brti WHERE src='1s' ORDER BY src_ts_ms")]
    gaps = sorted(((b - a, a, b) for a, b in zip(ts, ts[1:]) if b - a > GAP_MS), reverse=True)
    print(f"window   {iso(lo)} -> {iso(hi)}  ({(hi - lo) / 86_400_000:.2f} days)")
    print(f"brti     {sum(by_src.values()):,} rows ({joined(by_src)})")
    print(f"         1-second values in {minutes:,} distinct minutes ({minutes / 60:.1f} h)")
    if gaps:
        g, a, b = gaps[0]
        print(f"         {len(gaps)} gaps > {GAP_MS // 1000} s, {sum(x[0] for x in gaps) / 3.6e6:.1f} h "
              f"in total; longest {g / 3.6e6:.1f} h ({iso(a)} -> {iso(b)})")
    return sum(by_src.values())


def series_breakdown(db, have):
    queries = [("ticker", "ticker", ""), ("live trades", "trades", f"WHERE {LIVE_TRADE}"),
               ("book", "book", ""), ("backfill", "backfill_markets", "")]
    print("\nby series: rows (markets)")
    for label, table, where in queries:
        if table in have:
            rows = db.execute(f"SELECT {SERIES}, COUNT(*), COUNT(DISTINCT ticker) "
                              f"FROM {table} {where} GROUP BY 1 ORDER BY 1")
            print(f"  {label:<12} " + "   ".join(f"{s} {n:,} ({m:,})" for s, n, m in rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/kalshi.db")
    db = sqlite3.connect(f"file:{ap.parse_args().db}?mode=ro&immutable=1", uri=True)
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    live = brti_summary(db) if "brti" in have else 0

    if "trades" in have:
        total = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        n, mkts = db.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT ticker) FROM trades WHERE {LIVE_TRADE}").fetchone()
        live += n
        print(f"trades   {total:,} rows: live feed {n:,} ({mkts:,} markets), backfill {total - n:,}")
    if "ticker" in have:
        n, mkts = db.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM ticker").fetchone()
        live += n
        print(f"ticker   {n:,} rows ({mkts:,} markets)")
    if "book" in have:
        kinds = dict(db.execute("SELECT kind, COUNT(*) FROM book GROUP BY kind"))
        live += sum(kinds.values())
        print(f"book     {sum(kinds.values()):,} rows ({joined(kinds)})")
    if "markets" in have:
        n, mkts = db.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM markets").fetchone()
        print(f"markets  {n:,} discovery snapshots of {mkts:,} markets")
    for table in ("candles", "backfill_markets"):
        if table in have:
            print(f"{table:<8} {db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:,} rows")
    print(f"\nlive market-data rows (brti + live trades + ticker + book): {live:,}")
    series_breakdown(db, have)


if __name__ == "__main__":
    main()
