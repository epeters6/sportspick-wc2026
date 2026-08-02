from __future__ import annotations

import unittest
from datetime import datetime, timezone

from arbitrage.backend.calculator import calculate_arbitrage
from arbitrage.backend.config import settings
from arbitrage.backend.execution import (
    parse_kalshi_asks,
    parse_polymarket_asks,
    walk_ask_levels,
)
from arbitrage.backend.models import MatchedPair, NormalizedMarket


class OrderBookTests(unittest.TestCase):
    def test_kalshi_bid_ladders_become_complementary_asks(self):
        yes_asks, no_asks = parse_kalshi_asks({
            "orderbook_fp": {
                "yes_dollars": [["0.40", "20"], ["0.42", "10"]],
                "no_dollars": [["0.54", "15"], ["0.56", "5"]],
            }
        })
        self.assertAlmostEqual(yes_asks[0][0], 0.44)
        self.assertEqual(yes_asks[0][1], 5.0)
        self.assertAlmostEqual(no_asks[0][0], 0.58)
        self.assertEqual(no_asks[0][1], 10.0)

    def test_polymarket_asks_are_sorted(self):
        asks = parse_polymarket_asks({
            "asks": [{"price": "0.52", "size": "3"}, {"price": "0.50", "size": "2"}]
        })
        self.assertEqual(asks, [(0.5, 2.0), (0.52, 3.0)])

    def test_vwap_walks_visible_depth(self):
        vwap, filled = walk_ask_levels([(0.40, 5), (0.50, 5)], 8)
        self.assertEqual(filled, 8)
        self.assertAlmostEqual(vwap, 0.4375)

    def test_calculator_uses_asks_and_depth(self):
        now = datetime.now(timezone.utc)
        old_size = settings.quote_size_contracts
        settings.quote_size_contracts = 10
        try:
            kalshi = NormalizedMarket(
                platform="kalshi", market_id="K", title="Will X win?",
                event_title="Will X win?", category="politics",
                yes_price=0.2, no_price=0.8, volume=100,
                yes_asks=[(0.40, 10)], no_asks=[(0.62, 10)],
                quote_received_at=now,
            )
            poly = NormalizedMarket(
                platform="polymarket", market_id="P", condition_id="C",
                title="Will X win?", event_title="Will X win?", category="politics",
                yes_price=0.8, no_price=0.2, volume=100,
                yes_asks=[(0.45, 10)], no_asks=[(0.50, 10)],
                quote_received_at=now,
            )
            opportunity = calculate_arbitrage(MatchedPair(
                kalshi=kalshi, polymarket=poly, confidence=100,
                match_reasons=["hard_identity_passed"],
            ))
            self.assertEqual(opportunity.kalshi_buy_side, "yes")
            self.assertEqual(opportunity.polymarket_buy_side, "no")
            self.assertEqual(opportunity.executable_size, 10)
            self.assertAlmostEqual(opportunity.capital_required, 0.90)
            self.assertGreater(opportunity.net_gap, 0)
            self.assertTrue(opportunity.quote_valid)
        finally:
            settings.quote_size_contracts = old_size

    def test_calculator_fails_closed_without_books(self):
        market = NormalizedMarket(
            platform="kalshi", market_id="K", title="Will X win?",
            event_title="Will X win?", category="other",
            yes_price=0.5, no_price=0.5, volume=0,
        )
        with self.assertRaises(ValueError):
            calculate_arbitrage(MatchedPair(kalshi=market, polymarket=market, confidence=100))


if __name__ == "__main__":
    unittest.main()
