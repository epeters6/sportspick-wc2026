from __future__ import annotations

import unittest

from arbitrage.backend.matcher import compute_match_confidence
from arbitrage.backend.models import NormalizedMarket


def market(title: str, event: str = "", market_id: str = "m") -> NormalizedMarket:
    return NormalizedMarket(
        platform="test",
        market_id=market_id,
        title=title,
        event_title=event or title,
        category="politics",
        yes_price=0.5,
        no_price=0.5,
        volume=100,
    )


class SettlementIdentityTests(unittest.TestCase):
    def score(self, left: str, right: str) -> tuple[float, list[str]]:
        confidence, _, reasons = compute_match_confidence(market(left, market_id="a"), market(right, market_id="b"))
        return confidence, reasons

    def test_rejects_different_prime_minister_candidates_and_countries(self):
        confidence, reasons = self.score(
            "Will Yair Lapid be the next Prime Minister of Israel?",
            "Will Abiy Ahmed be the next Prime Minister of Ethiopia?",
        )
        self.assertEqual(confidence, 0)
        self.assertTrue(any("location_mismatch" in reason for reason in reasons))

    def test_rejects_supporting_vs_lead_award(self):
        confidence, reasons = self.score(
            "Will Sandra Huller win Best Supporting Actress at the Oscars?",
            "Will Sandra Huller win Best Actress at the 99th Academy Awards?",
        )
        self.assertEqual(confidence, 0)
        self.assertTrue(any("event_type_mismatch" in reason for reason in reasons))

    def test_rejects_different_endorsement_objects(self):
        confidence, reasons = self.score(
            "Will Donald Trump endorse no one in the 2028 presidential election?",
            "Will Donald Trump endorse J.D. Vance in the 2028 presidential election?",
        )
        self.assertEqual(confidence, 0)
        self.assertTrue(any("endorsement_object_mismatch" in reason for reason in reasons))

    def test_accepts_exact_binary_proposition(self):
        confidence, _, reasons = compute_match_confidence(
            market("Will Naftali Bennett be the next Prime Minister of Israel?", market_id="a"),
            market("Will Naftali Bennett be the next Prime Minister of Israel?", market_id="b"),
        )
        self.assertGreaterEqual(confidence, 85)
        self.assertIn("hard_identity_passed", reasons)

    def test_accepts_explicit_option_inversion(self):
        confidence, inverted, reasons = compute_match_confidence(
            market("Will OpenAI or Anthropic IPO first?", market_id="a"),
            market("Will Anthropic or OpenAI IPO first?", market_id="b"),
        )
        self.assertGreaterEqual(confidence, 85)
        self.assertTrue(inverted)
        self.assertIn("hard_identity_passed", reasons)


if __name__ == "__main__":
    unittest.main()
