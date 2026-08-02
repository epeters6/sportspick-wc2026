from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from arbitrage.backend import database
from arbitrage.backend.models import ArbitrageOpportunity


def opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        event_name="Event", kalshi_title="Will X win?", polymarket_title="Will X win?",
        kalshi_url=None, polymarket_url=None, match_confidence=100,
        kalshi_market_id="K", polymarket_market_id="P", inverted_outcomes=False,
        kalshi_yes=0.40, kalshi_no=0.62, polymarket_yes=0.45, polymarket_no=0.50,
        buy_yes_on="kalshi", buy_no_on="polymarket",
        kalshi_buy_side="yes", polymarket_buy_side="no",
        kalshi_leg_price=0.40, polymarket_leg_price=0.50,
        kalshi_fee=0.0168, polymarket_fee=0.01, total_fees=0.0268,
        execution_buffer=0.01,
        gross_gap=0.10, net_gap=0.0732, capital_required=0.90,
        roi_pct=8.13, executable_size=10,
        kalshi_volume=100, polymarket_volume=100, category="politics",
        timestamp="2026-08-01T12:00:00+00:00",
        match_reasons=["hard_identity_passed"], quote_valid=True,
    )


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, "test.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_expected_profit_is_not_credited_to_balance(self):
        bet_id = database.log_shadow_bet(opportunity(), size_contracts=10)
        self.assertIsNotNone(bet_id)
        summary = database.get_shadow_bets_summary()
        self.assertEqual(summary["current_balance"], summary["starting_balance"])
        self.assertEqual(summary["realized_pnl"], 0)
        self.assertGreater(summary["expected_open_pnl"], 0)

    def test_settlement_credits_only_realized_profit(self):
        bet_id = database.log_shadow_bet(opportunity(), size_contracts=10)
        self.assertTrue(database.settle_shadow_bet(bet_id, "yes", "yes"))
        summary = database.get_shadow_bets_summary()
        self.assertEqual(summary["open_paper_bets"], 0)
        self.assertAlmostEqual(summary["realized_pnl"], 1.0, places=2)
        self.assertAlmostEqual(summary["current_balance"], 10001.0, places=2)

    def test_disagreeing_venue_results_go_to_review(self):
        bet_id = database.log_shadow_bet(opportunity(), size_contracts=10)
        self.assertFalse(database.settle_shadow_bet(bet_id, "yes", "no"))
        conn = sqlite3.connect(database.DB_PATH)
        status = conn.execute("SELECT status FROM shadow_bets WHERE id=?", (bet_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "REVIEW")

    def test_history_uses_real_scan_ids_and_v2_only(self):
        scan_id = database.start_scan()
        database.log_opportunities([opportunity()], scan_id)
        database.finish_scan(
            scan_id,
            status="COMPLETED",
            kalshi_markets=10,
            polymarket_markets=12,
            strict_matches=1,
            executable_opportunities=1,
        )
        stats = database.get_history_stats()
        self.assertEqual(stats["total_cycles"], 1)
        self.assertEqual(stats["total_records"], 1)
        self.assertEqual(stats["profitable_records"], 1)
        self.assertEqual(stats["legacy_unverified_records"], 0)


if __name__ == "__main__":
    unittest.main()
