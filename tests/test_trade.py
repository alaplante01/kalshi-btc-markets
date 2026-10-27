"""Order construction and the client-side guards in trade.py. No network: HTTP is stubbed."""
import argparse
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import trade


def order_args(**overrides):
    args = dict(ticker="KXBTC15M-26SEP300100-00", count=50, at=0.55,
                taker=False, send=False, force=False)
    args.update(overrides)
    return argparse.Namespace(**args)


class TradeTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.group_file = Path(tmp.name) / "order-group.json"
        self.call = mock.Mock(return_value={"order": {"order_id": "o1"}})
        for name, value in (("GROUP_FILE", self.group_file), ("call", self.call)):
            patcher = mock.patch.object(trade, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_order(self, outcome, **overrides):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trade.order(None, order_args(**overrides), outcome)
        return out.getvalue()

    @staticmethod
    def payload(dry_run_output):
        return json.loads(dry_run_output[dry_run_output.index("{"):dry_run_output.rindex("}") + 1])


class DryRunTest(TradeTestCase):
    def test_maker_yes_is_a_post_only_bid(self):
        out = self.run_order("yes")
        body = self.payload(out)
        self.assertIn("DRY RUN", out)
        self.assertEqual((body["side"], body["price"], body["count"]), ("bid", "0.5500", "50.00"))
        self.assertTrue(body["post_only"])
        self.assertEqual(body["time_in_force"], "good_till_canceled")
        self.assertEqual(body["exchange_index"], trade.SHARD)
        self.call.assert_not_called()

    def test_no_is_an_ask_on_the_yes_book(self):
        body = self.payload(self.run_order("no", at=0.57))
        self.assertEqual((body["side"], body["price"]), ("ask", "0.4300"))

    def test_taker_is_immediate_or_cancel(self):
        body = self.payload(self.run_order("yes", taker=True))
        self.assertFalse(body["post_only"])
        self.assertEqual(body["time_in_force"], "immediate_or_cancel")

    def test_each_order_gets_a_fresh_client_order_id(self):
        ids = {self.payload(self.run_order("yes"))["client_order_id"] for _ in range(2)}
        self.assertEqual(len(ids), 2)

    def test_orders_join_the_saved_order_group(self):
        self.group_file.write_text(json.dumps({"order_group_id": "g-123", "limit": 400}))
        self.assertEqual(self.payload(self.run_order("yes"))["order_group_id"], "g-123")

    def test_count_cap_needs_force(self):
        with self.assertRaises(SystemExit):
            self.run_order("yes", count=trade.MAX_CONTRACTS + 1)
        body = self.payload(self.run_order("yes", count=trade.MAX_CONTRACTS + 1, force=True))
        self.assertEqual(body["count"], f"{trade.MAX_CONTRACTS + 1:.2f}")

    def test_group_and_cancels_send_nothing_without_send(self):
        with contextlib.redirect_stdout(io.StringIO()):
            trade.cmd_group(None, argparse.Namespace(limit=400, send=False))
            trade.cmd_cancel(None, argparse.Namespace(order_id="o1", market="M", send=False))
            trade.cmd_cancel_all(None, argparse.Namespace(send=False))
        self.assertFalse(self.group_file.exists())
        self.call.assert_not_called()


class GuardTest(TradeTestCase):
    def send(self, outcome, position, pnl, **overrides):
        with mock.patch.object(trade, "net_position", return_value=position), \
             mock.patch.object(trade, "pnl_24h", return_value=pnl) as pnl_24h:
            out = self.run_order(outcome, send=True, **overrides)
        return out, pnl_24h

    def test_sends_when_within_limits(self):
        self.send("yes", position=0.0, pnl=-10.0)
        method, path, body = self.call.call_args.args[1:]
        self.assertEqual((method, path), ("POST", "/trade-api/v2/portfolio/events/orders"))
        self.assertEqual(body["side"], "bid")

    def test_loss_brake_blocks_new_risk(self):
        with self.assertRaises(SystemExit) as cm:
            self.send("yes", position=0.0, pnl=-trade.DAILY_STOP)
        self.assertIn("BRAKE", str(cm.exception.code))
        self.call.assert_not_called()

    def test_loss_brake_lets_exits_through(self):
        out, pnl_24h = self.send("no", position=100.0, pnl=-500.0, count=50)
        pnl_24h.assert_not_called()  # reducing orders skip the P&L check entirely
        self.call.assert_called_once()
        self.assertIn("[reducing]", out)

    def test_flipping_through_flat_counts_as_new_risk(self):
        with self.assertRaises(SystemExit):
            self.send("no", position=20.0, pnl=-trade.DAILY_STOP - 1, count=100)
        self.call.assert_not_called()

    def test_net_position_cap(self):
        with self.assertRaises(SystemExit) as cm:
            self.send("yes", position=trade.NET_POSITION_CAP - 10.0, pnl=0.0, count=50)
        self.assertIn("net position", str(cm.exception.code))
        self.call.assert_not_called()


class RealizedPnlTest(unittest.TestCase):
    """pnl_24h() feeds the loss brake. Fills follow Kalshi's documented Fill shape."""

    def stub_api(self, fills):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        settlements = {"settlements": [
            {"ticker": "M1", "market_result": "yes", "settled_time": now},
            {"ticker": "OLD", "market_result": "no", "settled_time": "2020-01-01T00:00:00Z"}]}
        for f in fills:
            f.setdefault("created_time", now)

        def fake_get(_auth, path):
            return settlements if "settlements" in path else {"fills": fills, "cursor": None}

        return mock.patch.object(trade, "get", side_effect=fake_get)

    def test_buys_with_estimated_taker_fees(self):
        fills = [
            {"ticker": "M1", "outcome_side": "yes", "side": "yes", "action": "buy",
             "count_fp": "10.00", "yes_price_dollars": "0.4000", "is_taker": True},
            {"ticker": "M1", "outcome_side": "no", "side": "no", "action": "buy",
             "count_fp": "5.00", "yes_price_dollars": "0.7000", "is_taker": False},
            # settled more than 24 h ago: ignored
            {"ticker": "OLD", "outcome_side": "yes", "side": "yes", "action": "buy",
             "count_fp": "100.00", "yes_price_dollars": "0.5000", "is_taker": True},
        ]
        with self.stub_api(fills):
            pnl = trade.pnl_24h(None)
        cost = 10 * 0.40 + 5 * 0.30 + 0.07 * 10 * 0.40 * 0.60  # maker fill: no fee
        self.assertAlmostEqual(pnl, 10 - cost)  # YES settled: 10 contracts pay $1

    def test_direction_comes_from_outcome_side(self):
        """Fixed at publication: direction used to come from the legacy `side`, so a sell-YES
        fill (outcome_side "no") was booked as a YES purchase."""
        fills = [{"ticker": "M1", "outcome_side": "no", "side": "yes", "action": "sell",
                  "count_fp": "10.00", "yes_price_dollars": "0.6000", "is_taker": True}]
        with self.stub_api(fills):
            pnl = trade.pnl_24h(None)
        # Long 10 NO at 0.40 and YES settled: lose the $4.00 paid plus the taker fee.
        self.assertAlmostEqual(pnl, -(10 * 0.40 + 0.07 * 10 * 0.60 * 0.40))


if __name__ == "__main__":
    unittest.main()
