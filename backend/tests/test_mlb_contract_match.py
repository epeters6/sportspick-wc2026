"""Regression tests for MLB pitcher-outs contract matching."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.models.sports.mlb_contract_match import match_pitcher_outs_contract


def _mkt(question: str, outcomes: list[str], market_id: str = "m1"):
    return SimpleNamespace(
        question=question,
        market_id=market_id,
        outcomes=[SimpleNamespace(name=o, price=0.45, best_ask=0.46) for o in outcomes],
    )


class TestMlbContractMatch(unittest.TestCase):
    def test_correct_pitcher_line_side(self):
        markets = [
            _mkt(
                "Will Spencer Strider record over/under 17.5 outs?",
                ["Over", "Under"],
            )
        ]
        m = match_pitcher_outs_contract(
            markets=markets,
            pitcher_name="Spencer Strider",
            team="ATL",
            opponent="NYM",
            slate_date="2026-07-21",
            prop_line=17.5,
            prop_side="UNDER",
        )
        self.assertIsNone(m.rejection_reason)
        self.assertEqual(m.side, "UNDER")
        self.assertEqual(m.outcome.name, "Under")

    def test_wrong_date_pitcher_still_requires_outs_contract(self):
        markets = [
            _mkt("Atlanta Braves vs New York Mets Winner", ["Braves", "Mets"], "ml")
        ]
        m = match_pitcher_outs_contract(
            markets=markets,
            pitcher_name="Spencer Strider",
            team="ATL",
            opponent="NYM",
            slate_date="2026-07-21",
            prop_line=17.5,
            prop_side="UNDER",
        )
        self.assertEqual(m.rejection_reason, "NO_MATCHING_TARGET_CONTRACT")

    def test_moneyline_not_substituted(self):
        markets = [
            _mkt("Braves vs Mets moneyline", ["Braves", "Mets"]),
            _mkt("Spencer Strider outs 17.5", ["Over", "Under"]),
        ]
        m = match_pitcher_outs_contract(
            markets=markets,
            pitcher_name="Spencer Strider",
            team="ATL",
            opponent="NYM",
            slate_date="2026-07-21",
            prop_line=17.5,
            prop_side="OVER",
        )
        self.assertIsNone(m.rejection_reason)
        self.assertIn("outs", m.market.question.lower())
        self.assertEqual(m.outcome.name, "Over")

    def test_ambiguous_outcome_no_yes_fallback(self):
        markets = [
            _mkt("Spencer Strider outs 17.5", ["Team A", "Team B"]),
        ]
        m = match_pitcher_outs_contract(
            markets=markets,
            pitcher_name="Spencer Strider",
            team="ATL",
            opponent="NYM",
            slate_date="2026-07-21",
            prop_line=17.5,
            prop_side="UNDER",
        )
        self.assertEqual(m.rejection_reason, "AMBIGUOUS_OUTCOME_NO_YES_FALLBACK")

    def test_line_mismatch_rejected(self):
        markets = [_mkt("Spencer Strider outs 14.5", ["Over", "Under"])]
        m = match_pitcher_outs_contract(
            markets=markets,
            pitcher_name="Spencer Strider",
            team="ATL",
            opponent="NYM",
            slate_date="2026-07-21",
            prop_line=17.5,
            prop_side="UNDER",
        )
        self.assertEqual(m.rejection_reason, "NO_MATCHING_TARGET_CONTRACT")


if __name__ == "__main__":
    unittest.main()
