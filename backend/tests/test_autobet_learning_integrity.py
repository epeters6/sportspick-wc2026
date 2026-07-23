from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.trading.autobet_learning import assess_live_readiness, gates_for_price


def _settings(**overrides):
    values = {
        "polymarket_live_min_settled_bets": 1,
        "polymarket_live_min_roi_pct": 0.0,
        "polymarket_paper_loose_gates": True,
        "polymarket_longshot_min_edge_paper": 0.08,
        "polymarket_underdog_min_edge_paper": 0.06,
        "polymarket_paper_min_edge": 0.05,
        "polymarket_min_edge": 0.05,
        "polymarket_longshot_min_edge_live": 0.08,
        "polymarket_underdog_min_edge_live": 0.06,
        "polymarket_longshot_min_model_prob": 0.20,
        "polymarket_underdog_min_model_prob": 0.25,
        "polymarket_coinflip_min_model_prob": 0.30,
        "polymarket_favorite_min_model_prob": 0.35,
        "polymarket_bankroll": 1000.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestAutobetLearningIntegrity(unittest.TestCase):
    def test_readiness_excludes_invalid_rows(self):
        verified = {
            "strategy": "legacy_consensus_mlb",
            "sport": "mlb",
            "mode": "paper",
            "stake": 10.0,
            "pnl": 5.0,
        }
        invalid = {
            **verified,
            "id": "bad-positive",
            "pnl": 100.0,
            "_integrity_reason": "SETTLEMENT_PNL_MISMATCH",
        }
        with patch(
            "backend.trading.autobet_learning.settlement_integrity_datasets",
            return_value={
                "verified_rows": [verified],
                "invalid_rows": [invalid],
                "unverifiable_rows": [],
            },
        ), patch(
            "backend.trading.autobet_learning.get_settings",
            return_value=_settings(),
        ):
            readiness = assess_live_readiness()
        self.assertFalse(readiness["live_ready"])
        self.assertEqual(readiness["verified_settled_bets"], 1)
        self.assertEqual(readiness["integrity_excluded_count"], 1)
        self.assertEqual(
            readiness["integrity_exclusion_reasons"]["SETTLEMENT_PNL_MISMATCH"],
            1,
        )

    def test_incorrect_positive_pnl_cannot_enable_loose_gate(self):
        invalid = {
            "sport": "mlb",
            "strategy": "legacy_consensus_mlb",
            "market_price": 0.45,
            "stake": 10.0,
            "pnl": -10.0,
            "resolved_at": "2026-07-23T12:00:00+00:00",
        }
        with patch(
            "backend.trading.autobet_learning.get_settings",
            return_value=_settings(),
        ), patch(
            "backend.trading.autobet_learning._get_live_bankroll",
            return_value=1000.0,
        ), patch(
            "backend.trading.autobet_learning._conservative_settled_rows",
            return_value=[invalid],
        ), patch(
            "backend.trading.autobet_learning._integrity_exclusions_for",
            return_value=[invalid],
        ), patch(
            "backend.trading.autobet_learning.compute_tier_stats",
            return_value={},
        ), patch(
            "backend.trading.autobet_learning.compute_upset_trap_stats",
            return_value={"upset_trap": {"settled": 0}},
        ), patch(
            "backend.trading.autobet_learning.compute_sport_stats",
            return_value={},
        ):
            gates = gates_for_price(0.45, paper=True, sport="mlb")
        self.assertGreaterEqual(gates.min_edge, 0.05)
        self.assertGreater(gates.min_edge, 0.005)


if __name__ == "__main__":
    unittest.main()
