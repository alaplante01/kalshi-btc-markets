"""Fee, fair-value model and signal rule in terminal.py."""
import math
import unittest

import terminal


class TakerFeeTest(unittest.TestCase):
    def test_peaks_at_even_odds(self):
        self.assertAlmostEqual(terminal.taker_fee(0.50), 0.0175)

    def test_symmetric_and_zero_at_the_bounds(self):
        self.assertAlmostEqual(terminal.taker_fee(0.10), terminal.taker_fee(0.90))
        self.assertEqual(terminal.taker_fee(0.0), 0.0)
        self.assertEqual(terminal.taker_fee(1.0), 0.0)


class ModelBeforeSettlementWindowTest(unittest.TestCase):
    def test_at_the_strike_is_a_coin_flip(self):
        p, sd = terminal.model_p(100.0, 100.0, sigma=2.0, t_left=630, sa=None, sn=None)
        self.assertAlmostEqual(p, 0.5)
        self.assertAlmostEqual(sd, 2.0 * math.sqrt(600))  # horizon is t_left - 30 s

    def test_one_sd_above_the_strike(self):
        sd = 2.0 * math.sqrt(600)
        p, _ = terminal.model_p(100.0 + sd, 100.0, sigma=2.0, t_left=630, sa=None, sn=None)
        self.assertAlmostEqual(p, 0.8413, places=4)

    def test_probability_rises_with_spot(self):
        ps = [terminal.model_p(v, 100.0, 1.0, 300, None, None)[0] for v in (95, 99, 100, 101, 105)]
        self.assertEqual(ps, sorted(ps))

    def test_horizon_is_clamped_to_one_second(self):
        _, sd = terminal.model_p(100.0, 100.0, sigma=3.0, t_left=10, sa=None, sn=None)
        self.assertAlmostEqual(sd, 3.0)


class ModelInsideSettlementWindowTest(unittest.TestCase):
    def test_projects_the_running_average(self):
        # 40 of the 60 settlement values are in, averaging 99.0; spot is 101.0.
        p, sd = terminal.model_p(101.0, 100.0, sigma=1.5, t_left=20, sa=99.0, sn=40)
        projected = (99.0 * 40 + 101.0 * 20) / 60
        self.assertAlmostEqual(sd, 1.5 * math.sqrt(20 ** 3 / 3) / 60)
        self.assertAlmostEqual(p, terminal.phi((projected - 100.0) / sd))
        self.assertLess(p, 0.5)  # spot is above the strike, the projected average is not

    def test_uncertainty_shrinks_as_values_arrive(self):
        sds = [terminal.model_p(100.0, 100.0, 1.0, 60 - n, 100.0, n)[1] for n in (1, 15, 30, 45, 59)]
        self.assertEqual(sds, sorted(sds, reverse=True))

    def test_settled_once_all_sixty_values_are_in(self):
        self.assertEqual(terminal.model_p(90.0, 100.0, 1.0, 0, 100.5, 60), (1.0, 0.0))
        self.assertEqual(terminal.model_p(110.0, 100.0, 1.0, 0, 99.5, 60), (0.0, 0.0))


class SignalRuleTest(unittest.TestCase):
    EDGE = 0.08

    def signal(self, p, bid, ask, t_left=600, sn=None, sd=10.0):
        return terminal.build_signal(p, sd, bid, ask, self.EDGE, sn, t_left)

    def test_take_yes_needs_the_fee_plus_the_edge(self):
        ask = 0.50
        needed = ask + terminal.taker_fee(ask) + self.EDGE
        sig, fire = self.signal(needed + 0.001, 0.48, ask)
        self.assertTrue(fire)
        self.assertTrue(sig.startswith("TAKE YES @0.50"))
        self.assertEqual(self.signal(needed - 0.001, 0.48, ask), ("HOLD", False))

    def test_take_no_is_priced_off_the_yes_bid(self):
        sig, fire = self.signal(p=0.40, bid=0.60, ask=0.62)  # NO costs 1 - 0.60
        self.assertTrue(fire)
        self.assertTrue(sig.startswith("TAKE NO @0.40"))

    def test_last_two_minutes_hold_without_a_lock(self):
        self.assertTrue(self.signal(0.70, 0.48, 0.50, t_left=600)[1])
        self.assertEqual(self.signal(0.70, 0.48, 0.50, t_left=90), ("HOLD", False))

    def test_locked_up_inside_the_settlement_window(self):
        sig, fire = self.signal(0.9995, 0.90, 0.92, t_left=20, sn=40, sd=0.5)
        self.assertTrue(fire)
        self.assertTrue(sig.startswith("LOCKED UP"))

    def test_locked_down_inside_the_settlement_window(self):
        sig, fire = self.signal(0.0005, 0.08, 0.10, t_left=20, sn=40, sd=0.5)  # NO costs 0.92
        self.assertTrue(fire)
        self.assertTrue(sig.startswith("LOCKED DOWN"))

    def test_no_lock_once_the_market_prices_it_in(self):
        self.assertEqual(self.signal(0.9995, 0.95, 0.96, t_left=20, sn=40, sd=0.5), ("HOLD", False))


if __name__ == "__main__":
    unittest.main()
