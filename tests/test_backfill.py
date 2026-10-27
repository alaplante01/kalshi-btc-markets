"""REST backfill helpers in backfill.py."""
import sqlite3
import unittest
from unittest import mock

import backfill


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(backfill.SCHEMA)
        self.addCleanup(self.db.close)

    def test_iso_to_ts_ignores_fractions_and_zone(self):
        self.assertEqual(backfill.iso_to_ts("2026-09-29T04:56:30Z"), 1_790_657_790)
        self.assertEqual(backfill.iso_to_ts("2026-09-29T04:56:30.987654Z"), 1_790_657_790)

    def test_backfilled_trades_have_whole_second_timestamps(self):
        """capture_stats.py separates live from backfilled trades on this."""
        trade = {"trade_id": "x", "ticker": "KXBTC15M-26SEP290100-00",
                 "created_time": "2026-09-29T04:56:30.987Z", "yes_price_dollars": "0.80",
                 "no_price_dollars": "0.20", "count_fp": "50.00", "taker_side": "no",
                 "taker_outcome_side": "no", "taker_book_side": "ask"}
        backfill.save_trades(self.db, [trade, trade])
        self.assertEqual(self.db.execute("SELECT trade_id, ts_ms FROM trades").fetchall(),
                         [("x", 1_790_657_790_000)])

    def test_backoff_retries_rate_limits(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            return "ok"

        with mock.patch.object(backfill.time, "sleep") as sleep:
            self.assertEqual(backfill.with_backoff(flaky), "ok")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15])

    def test_backoff_raises_other_errors_at_once(self):
        with mock.patch.object(backfill.time, "sleep") as sleep:
            with self.assertRaises(ValueError):
                backfill.with_backoff(mock.Mock(side_effect=ValueError("bad request")))
        sleep.assert_not_called()

    def test_backoff_gives_up_after_the_last_retry(self):
        fn = mock.Mock(side_effect=RuntimeError("429"))
        with mock.patch.object(backfill.time, "sleep"):
            with self.assertRaises(RuntimeError):
                backfill.with_backoff(fn)
        self.assertEqual(fn.call_count, backfill.RETRIES)


if __name__ == "__main__":
    unittest.main()
