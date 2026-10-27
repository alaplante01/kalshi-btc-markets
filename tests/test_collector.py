"""Message parsing, the single SQLite writer, the market feed handler and discovery."""
import asyncio
import contextlib
import gc
import io
import json
import os
import sqlite3
import tempfile
import unittest
import warnings
from unittest import mock

import collector


def trade_msg(trade_id="t1", **overrides):
    msg = {"trade_id": trade_id, "market_ticker": "KXBTC15M-26SEP290100-00",
           "ts_ms": 1_790_657_790_123, "yes_price_dollars": "0.5300",
           "no_price_dollars": "0.4700", "count_fp": "12.00", "taker_side": "yes",
           "taker_outcome_side": "yes", "taker_book_side": "bid", "is_block_trade": False}
    msg.update(overrides)
    return msg


class ParsingTest(unittest.TestCase):
    def test_fnum(self):
        self.assertIsNone(collector.fnum(None))
        self.assertIsNone(collector.fnum(""))
        self.assertEqual(collector.fnum("0.5300"), 0.53)
        self.assertEqual(collector.fnum(0), 0.0)

    def test_brti_row_keeps_the_running_settlement_average(self):
        msg = {"received_at": 1_790_657_790_650,
               "data": json.dumps({"time": 1_790_657_790_600, "value": "112001.25"}),
               "avg_60s_data": {"value": "111990.10", "window_size": 60},
               "last_60s_windowed_average_15min": {"value": "111985.00", "window_size": 17}}
        self.assertEqual(collector.brti_row(msg, "1s"),
                         (1_790_657_790_650, 1_790_657_790_600, 112001.25,
                          111990.10, 60, 111985.00, 17, "1s"))

    def test_brti_row_outside_the_settlement_minute(self):
        msg = {"received_at": 1, "data": json.dumps({"time": 2, "value": "100.0"})}
        self.assertEqual(collector.brti_row(msg, "5hz"),
                         (1, 2, 100.0, None, None, None, None, "5hz"))

    def test_trade_row(self):
        self.assertEqual(collector.trade_row(trade_msg(is_block_trade=True)),
                         ("t1", "KXBTC15M-26SEP290100-00", 1_790_657_790_123,
                          0.53, 0.47, 12.0, "yes", "yes", "bid", 1))

    def test_ticker_row_tolerates_missing_fields(self):
        row = collector.ticker_row({"ts_ms": 5, "market_ticker": "X", "yes_bid_dollars": "0.41"})
        self.assertEqual(row, (5, "X", None, 0.41, None, None, None, None, None, None))


class WriterTest(unittest.TestCase):
    def test_time_based_commit_and_trade_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            async def scenario():
                queue = asyncio.Queue()
                task = asyncio.create_task(collector.writer(db_path, queue))
                for trade_id in ("a", "a", "b"):  # "a" arrives twice
                    queue.put_nowait(("trade", collector.trade_row(trade_msg(trade_id))))
                queue.put_nowait(("ticker", collector.ticker_row({"ts_ms": 1, "market_ticker": "X"})))
                # Far fewer than COMMIT_EVERY_ROWS rows, so only the timer can commit them.
                await asyncio.sleep(collector.COMMIT_EVERY_SECS * 3)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            # writer() owns its connection for the life of the process and never closes it.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                asyncio.run(scenario())
                gc.collect()

            con = sqlite3.connect(db_path)
            self.addCleanup(con.close)
            self.assertEqual(con.execute("SELECT trade_id FROM trades ORDER BY 1").fetchall(),
                             [("a",), ("b",)])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ticker").fetchone(), (1,))
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone(), ("wal",))


class FakeSocket:
    """Replays scripted frames like a websockets connection, then requests a rebuild."""

    def __init__(self, frames, rebuild_evt):
        self.frames = [json.dumps(f) for f in frames]
        self.sent = []
        self.rebuild_evt = rebuild_evt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self.frames:
            self.rebuild_evt.set()
            return json.dumps({"type": "ok"})
        return self.frames.pop(0)


class MarketFeedTest(unittest.TestCase):
    def test_enqueues_rows_and_logs_sequence_gaps(self):
        frames = [
            {"type": "trade", "msg": trade_msg("x")},
            {"type": "ticker", "msg": {"ts_ms": 1, "market_ticker": "A", "yes_bid_dollars": "0.40"}},
            {"type": "orderbook_snapshot", "sid": 2, "seq": 1, "msg": {"market_ticker": "A"}},
            {"type": "orderbook_delta", "sid": 2, "seq": 2, "msg": {"market_ticker": "A"}},
            {"type": "orderbook_delta", "sid": 2, "seq": 5, "msg": {"market_ticker": "A"}},
        ]

        async def scenario():
            queue, rebuild = asyncio.Queue(), asyncio.Event()
            sock = FakeSocket(frames, rebuild)

            async def connect(_auth):
                return sock

            with mock.patch.object(collector, "ws_connect", connect):
                await collector.market_loop(None, queue, ["A", "B"], ["A"], rebuild)
            return sock, [queue.get_nowait() for _ in range(queue.qsize())]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sock, items = asyncio.run(scenario())

        self.assertEqual([s["params"]["channels"] for s in sock.sent],
                         [["ticker", "trade"], ["orderbook_delta"]])
        self.assertEqual(sock.sent[0]["params"]["market_tickers"], ["A", "B"])
        self.assertEqual([kind for kind, _ in items], ["trade", "ticker", "book", "book", "book"])
        _, (_local_ms, ticker, kind, payload) = items[2]
        self.assertEqual((ticker, kind, json.loads(payload)),
                         ("A", "orderbook_snapshot", {"market_ticker": "A"}))
        gaps = [line for line in out.getvalue().splitlines() if "seq gap" in line]
        self.assertEqual(len(gaps), 1)
        self.assertIn("sid=2: 2 -> 5", gaps[0])


class FakeREST:
    def __init__(self, markets_by_series):
        self.markets_by_series = markets_by_series
        self.params = []

    def paged(self, path, params=None, list_key=None, **_):
        self.params.append(params)
        return [{"markets": self.markets_by_series.get(params["series_ticker"], [])}]


def mkt(ticker, status="active", bid="0.40", ask="0.45"):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": status,
            "yes_bid_dollars": bid, "yes_ask_dollars": ask}


HOURLY = [mkt(f"KXBTC-26SEP2917-B{110000 + 250 * i}") for i in range(60)]
FIFTEEN = mkt("KXBTC15M-26SEP291715-15")


class DiscoveryTest(unittest.TestCase):
    def test_tracks_active_markets_of_open_events(self):
        rest = FakeREST({"KXBTC15M": [FIFTEEN, mkt("KXBTC15M-26SEP291700-00", status="closed")]})
        all_t, books, rows = collector.discover(rest, ["KXBTC15M"])
        self.assertEqual(all_t, [FIFTEEN["ticker"]])
        self.assertEqual(books, [FIFTEEN["ticker"]])
        self.assertEqual([r[1] for r in rows], [FIFTEEN["ticker"]])
        self.assertEqual(rest.params[0]["status"], "open")

    def test_caps_order_book_subscriptions(self):
        all_t, books, _ = collector.discover(FakeREST({"KXBTC": HOURLY}), ["KXBTC"])
        self.assertEqual(len(all_t), len(HOURLY))
        self.assertEqual(len(books), collector.BOOK_CAP)

    def test_15_minute_market_keeps_a_book_slot_when_the_cap_is_full(self):
        """Fixed at publication: the cap used to be applied to an alphabetical list, and every
        KXBTC-... ticker sorts before KXBTC15M-..., so the 15-minute markets got no books."""
        rest = FakeREST({"KXBTC15M": [FIFTEEN], "KXBTC": HOURLY})
        _, books, _ = collector.discover(rest, ["KXBTC15M", "KXBTC"])
        self.assertIn(FIFTEEN["ticker"], books)


if __name__ == "__main__":
    unittest.main()
