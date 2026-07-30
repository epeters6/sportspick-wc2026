"""Exchange weather order-book depth normalization."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from backend.models.sports.run_shadow_mlb import shrink_mlb_probability
from pavlov.pipeline.kalshi_client import (
    _parse_market,
    _top_of_book_from_orderbook_fp,
)
from pavlov.polymarket.poly_client import (
    _bbo_from_book,
    _build_price_bbo,
    _normalize_market_row,
)


class TestKalshiWeatherDepth(unittest.TestCase):
    def test_market_response_fixed_point_ask_size_is_preserved(self):
        row = _parse_market(
            {
                "ticker": "KXHIGHNY-TEST",
                "yes_ask_dollars": "0.4400",
                "yes_bid_dollars": "0.4200",
                "yes_ask_size_fp": "17.50",
                "yes_bid_size_fp": "13.00",
            },
            "KXHIGHNY",
        )
        self.assertEqual(row["yes_ask"], 44)
        self.assertEqual(row["ask_size"], 17.5)
        self.assertEqual(row["yes_ask_qty"], 17.5)

    def test_orderbook_no_bid_becomes_yes_ask_with_same_depth(self):
        bbo = _top_of_book_from_orderbook_fp(
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.4200", "13.00"]],
                    "no_dollars": [
                        ["0.5500", "4.00"],
                        ["0.5600", "17.00"],
                    ],
                }
            }
        )
        self.assertEqual(bbo["yes_bid"], 42)
        self.assertEqual(bbo["yes_ask"], 44)
        self.assertEqual(bbo["ask_size"], 17.0)


class TestPolymarketWeatherDepth(unittest.TestCase):
    BOOK = {
        "marketData": {
            "bids": [
                {"px": {"value": "0.40"}, "qty": "12"},
                {"px": {"value": "0.42"}, "qty": "13"},
            ],
            "offers": [
                {"px": {"value": "0.46"}, "qty": "7"},
                {"px": {"value": "0.44"}, "qty": "17"},
            ],
        }
    }

    def test_book_extracts_best_offer_quantity(self):
        bbo = _bbo_from_book(self.BOOK)
        self.assertEqual(bbo["bestBid"]["value"], "0.42")
        self.assertEqual(bbo["bestAsk"]["value"], "0.44")
        self.assertEqual(bbo["ask_size"], 17.0)

    def test_bbo_price_without_size_fetches_full_book(self):
        client = MagicMock()
        client.markets.bbo.return_value = {
            "marketData": {"bestBid": {"value": "0.42"}, "bestAsk": {"value": "0.44"}}
        }
        client.markets.book.return_value = self.BOOK
        bbo = _build_price_bbo(client, {}, "weather-slug")
        client.markets.book.assert_called_once_with("weather-slug")
        self.assertEqual(bbo["ask_size"], 17.0)

    def test_normalized_row_preserves_executable_depth(self):
        client = MagicMock()
        bbo = _bbo_from_book(self.BOOK)
        row = _normalize_market_row(
            client,
            {
                "slug": "nyc-high-80",
                "eventSlug": "",
                "title": "Will the high temperature in NYC be 80 to 81 degrees Fahrenheit?",
                "endDate": "2026-07-30T23:00:00Z",
            },
            bbo,
            display_text=(
                "Will the high temperature in NYC be 80 to 81 degrees Fahrenheit?"
            ),
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["ask_size"], 17.0)


class TestMlbMarketShrink(unittest.TestCase):
    def test_uncalibrated_probability_is_shrunk_to_market(self):
        adjusted, baseline = shrink_mlb_probability(
            0.70, 0.48, 0.52, model_weight=0.35
        )
        self.assertAlmostEqual(baseline, 0.50)
        self.assertAlmostEqual(adjusted, 0.57)

    def test_crossed_book_rejected(self):
        with self.assertRaisesRegex(ValueError, "CROSSED_ORDERBOOK"):
            shrink_mlb_probability(0.60, 0.55, 0.50)


if __name__ == "__main__":
    unittest.main()
