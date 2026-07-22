"""Hand-checked dollar-fraction Kelly log-growth units."""
from __future__ import annotations

import math
import unittest

from pavlov.pipeline.binary_kelly import (
    binary_kelly_fraction,
    expected_binary_log_growth,
)
from pavlov.pipeline.portfolio_optimizer import expected_log_growth
import numpy as np


class TestKellyUnits(unittest.TestCase):
    def test_hand_calculated_log_growth(self):
        # f=0.1 dollars/bankroll, p=0.6, c=0.5
        # wealth_win = 1 - 0.1 + 0.1/0.5 = 1.1
        # wealth_loss = 1 - 0.1 = 0.9
        # E[log] = 0.6*log(1.1) + 0.4*log(0.9)
        f, p, c = 0.1, 0.6, 0.5
        expected = 0.6 * math.log(1.1) + 0.4 * math.log(0.9)
        self.assertAlmostEqual(expected_binary_log_growth(f, p, c), expected, places=12)

    def test_old_share_fraction_formula_is_wrong_for_dollar_f(self):
        """Guard: dollar-f must not use the share-fraction wealth formula."""
        f, p, c = 0.1, 0.6, 0.5
        wrong = p * math.log(1.0 + f * (1.0 - c)) + (1.0 - p) * math.log(1.0 - f * c)
        correct = expected_binary_log_growth(f, p, c)
        self.assertNotAlmostEqual(wrong, correct, places=6)

    def test_p_le_c_kelly_fraction_zero(self):
        self.assertEqual(binary_kelly_fraction(0.4, 0.5, "YES"), 0.0)
        self.assertEqual(binary_kelly_fraction(0.5, 0.5, "YES"), 0.0)
        self.assertEqual(expected_binary_log_growth(0.0, 0.4, 0.5), 0.0)

    def test_yes_no_symmetry(self):
        # Model P(YES)=0.4 → P(NO)=0.6; NO cost 0.5 → same Kelly as YES at 0.6 / 0.5
        f_yes = binary_kelly_fraction(0.6, 0.5, "YES")
        f_no = binary_kelly_fraction(0.4, 0.5, "NO")
        self.assertAlmostEqual(f_yes, f_no)
        self.assertAlmostEqual(f_yes, (0.6 - 0.5) / (1.0 - 0.5))

        g_yes = expected_binary_log_growth(f_yes, 0.6, 0.5)
        g_no = expected_binary_log_growth(f_no, 0.6, 0.5)
        self.assertAlmostEqual(g_yes, g_no)

    def test_guards(self):
        self.assertEqual(expected_binary_log_growth(-0.1, 0.6, 0.5), 0.0)
        self.assertEqual(expected_binary_log_growth(0.1, 0.6, 0.0), 0.0)
        self.assertEqual(expected_binary_log_growth(0.1, 0.6, 1.0), 0.0)
        self.assertEqual(expected_binary_log_growth(1.0, 0.6, 0.5), -float("inf"))
        self.assertEqual(expected_binary_log_growth(1.5, 0.6, 0.5), -float("inf"))

    def test_portfolio_optimizer_dollar_fraction_semantics(self):
        """x shares at cost q → dollars = x*q; relative wealth matches binary formula."""
        bankroll = 1000.0
        x = np.array([20.0, 0.0])  # 20 shares of bucket 0
        q = np.array([0.5, 0.5])
        p = np.array([0.6, 0.4])
        dollars = float(np.sum(q * x))  # 10
        f = dollars / bankroll  # 0.01
        # Outcome 0 wins: wealth = B - dollars + x0 = 1000 - 10 + 20 = 1010 → 1.01
        # = 1 - f + f/c with c=0.5: 1 - 0.01 + 0.01/0.5 = 1.01
        neg_log = expected_log_growth(x, p, q, bankroll)
        expected_abs = p[0] * math.log(1010.0) + p[1] * math.log(990.0)
        self.assertAlmostEqual(-neg_log, expected_abs, places=10)
        # Delta vs no-trade equals binary dollar-fraction growth for single-bucket bet
        delta = -neg_log - math.log(bankroll)
        binary_delta = expected_binary_log_growth(f, 0.6, 0.5)
        self.assertAlmostEqual(delta, binary_delta, places=10)


if __name__ == "__main__":
    unittest.main()
