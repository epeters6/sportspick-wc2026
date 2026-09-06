"""Current official fee examples, partial-fill budgets, and series overrides."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from pavlov.pipeline.fee_model import estimate_trade_fee, estimate_fee_per_share, weather_fee_metadata
from pavlov.pipeline.execution_cost import generate_executable_cost_vector


class TestWeatherFeeModel(unittest.TestCase):
    def test_polymarket_us_official_thousand_contract_examples(self):
        for price, expected in [(0.1, 5.40), (0.65, 13.65), (0.3, 12.60), (0.9, 5.40), (0.5, 15.00)]:
            with self.subTest(price=price):
                self.assertEqual(estimate_trade_fee("polymarket_us", price, 1000)["fee_total"], expected)

    def test_polymarket_us_bankers_rounding_and_symmetry(self):
        self.assertEqual(estimate_trade_fee("polymarket_us", 0.25, 100)["fee_total"], 1.12)
        self.assertEqual(estimate_trade_fee("polymarket_us", 0.75, 100)["fee_total"], 1.12)
        self.assertEqual(estimate_trade_fee("polymarket_us", 0.5, 1)["fee_total"], 0.02)
        self.assertEqual(estimate_trade_fee("polymarket_us", 0.01, 1)["fee_total"], 0)

    def test_whole_contract_budget_never_invents_zero_fee_large_order(self):
        for venue in ["polymarket_us", "kalshi"]:
            for price in [0.01, 0.055, 0.10, 0.25, 0.5, 0.65, 0.9, 0.99]:
                for shares in [1, 2, 3, 100, 1000]:
                    with self.subTest(venue=venue, price=price, shares=shares):
                        conservative = estimate_trade_fee(venue, price, shares, conservative=True)["fee_total"]
                        single = estimate_trade_fee(venue, price, shares)["fee_total"]
                        self.assertGreaterEqual(conservative + 1e-9, single)
        self.assertGreater(estimate_fee_per_share("polymarket_us", 0.01, 1), 0)

    def test_kalshi_official_non_direct_fractional_price_example(self):
        # Official example: revenue -.055, exact model fee .00363825,
        # trade fee .003639, rounding fee .001361, total .005.
        result = estimate_trade_fee("kalshi", 0.055, 1)
        self.assertAlmostEqual(result["fee_exact_unrounded"], 0.00363825)
        self.assertEqual(result["fee_total"], 0.005)

    def test_kalshi_precision_and_series_multiplier(self):
        self.assertEqual(estimate_trade_fee("kalshi", 0.5, 100, fee_multiplier=2.5)["fee_total"], 4.38)
        self.assertEqual(estimate_trade_fee("kalshi", 0.5, 100, fee_multiplier=2.5, balance_precision=0.0001)["fee_total"], 4.375)
        self.assertEqual(estimate_trade_fee("kalshi", 0.5, 100, fee_multiplier=0)["fee_total"], 0)

    def test_maker_rebates_are_not_assumed_as_profit(self):
        self.assertEqual(estimate_trade_fee("polymarket_us", 0.5, 100, liquidity_role="maker")["fee_total"], 0)
        self.assertGreater(estimate_trade_fee("kalshi", 0.5, 100, liquidity_role="maker", fee_type="quadratic_with_maker_fees")["fee_total"], 0)

    def test_invalid_unknown_or_unverified_fees_fail_closed(self):
        for args, kwargs in [
            (("international", 0.5, 1), {}), (("kalshi", 0.5, 1), {"fee_type": "flat"}),
            (("kalshi", 0.5, 1), {"fee_multiplier": -1}), (("kalshi", 0.5, 1), {"fee_multiplier": float("nan")}),
            (("polymarket", 0.5, 1), {"api_fee_override": -0.1}), (("polymarket", 0.5, 1), {"api_fee_override": float("inf")}),
            (("polymarket", float("nan"), 1), {}), (("polymarket", 1.1, 1), {}),
            (("polymarket", 0, 1), {}), (("kalshi", 0.5, -1), {}),
            (("kalshi", 0.5, 0.1), {"conservative": True}),
        ]:
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ValueError):
                estimate_trade_fee(*args, **kwargs)
        with self.assertRaisesRegex(ValueError, "UNVERIFIED"):
            weather_fee_metadata("kalshi", 0.5, {})

    def test_execution_cost_persists_fee_provenance_and_uses_override(self):
        market = {"best_ask": 0.5, "ask_size": 10, "fee_type": "quadratic", "fee_multiplier": 2,
                  "fee_source": "https://example.invalid/official-series-fixture"}
        costs, caps = generate_executable_cost_vector([market], "kalshi")
        self.assertEqual(costs, [0.545])
        self.assertEqual(caps, [10])
        self.assertEqual(market["fee_estimate"]["fee_venue_product"], "kalshi")
        self.assertEqual(market["fee_estimate"]["fee_rounding_policy"], "whole_contract_conservative_budget")
        with self.assertRaises(ValueError):
            generate_executable_cost_vector([{**market, "fee_type": "flat"}], "kalshi")


class TestKalshiWeatherFeeLookup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("PAVLOV_BYPASS_CONFIG", "1")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pavlov"))
        from pavlov.pipeline import kalshi_client
        cls.client = kalshi_client

    def setUp(self):
        self.client._FEE_CACHE.clear()

    def tearDown(self):
        self.client._FEE_CACHE.clear()

    def test_one_cent_kalshi_book_has_unambiguous_dollar_quote(self):
        # Exact response observed in the public preflight on September 6.
        book = self.client._top_of_book_from_orderbook_fp({"orderbook_fp": {
            "no_dollars": [["0.9900", "187367.51"]], "yes_dollars": [],
        }})
        self.assertEqual(book["yes_ask"], 1.0)  # Legacy cents alias retained.
        self.assertEqual(book["best_ask"], 0.01)
        book.update({"fee_type":"quadratic", "fee_multiplier":1, "fee_source":"official_test_series"})
        costs, depth = generate_executable_cost_vector([book], "kalshi")
        self.assertAlmostEqual(costs[0], 0.025)
        self.assertEqual(depth[0], 187367.51)
        listing = self.client._parse_market({"ticker":"KXHIGHNY-26SEP06-T73", "yes_ask_dollars":"0.0100", "yes_bid_dollars":"0.0050"})
        self.assertEqual(listing["best_ask"], 0.01)
        self.assertEqual(listing["best_bid"], 0.005)

    def test_nonfinite_book_levels_do_not_create_liquidity(self):
        book = self.client._top_of_book_from_orderbook_fp({"orderbook_fp": {
            "no_dollars": [["NaN", "100"], ["0.5", "Infinity"]], "yes_dollars": [],
        }})
        self.assertIsNone(book["best_ask"])
        self.assertEqual(book["ask_size"], 0)

    def test_fee_cache_shares_series_and_event_between_buckets(self):
        calls = []
        def fetch(path, params=None, *, signed=True):
            self.assertFalse(signed)
            calls.append((path, params))
            if path.startswith("/series/"):
                return {"series": {"ticker": "KXHIGHCHI", "fee_type": "quadratic", "fee_multiplier": 1}}
            return {"event_fee_changes": [
                {"event_ticker": "KXHIGHCHI-26SEP06", "series_ticker": "KXHIGHCHI", "scheduled_ts": "2020-01-01T00:00:00Z", "fee_type_override": "quadratic", "fee_multiplier_override": 2},
                {"event_ticker": "KXHIGHCHI-26SEP06", "series_ticker": "KXHIGHCHI", "scheduled_ts": "2999-01-01T00:00:00Z", "fee_type_override": "flat", "fee_multiplier_override": 3},
            ], "cursor": ""}
        with patch.object(self.client, "_get", side_effect=fetch):
            first = self.client.get_weather_fee_metadata("KXHIGHCHI-26SEP06-B82.5")
            second = self.client.get_weather_fee_metadata("KXHIGHCHI-26SEP06-B84.5")
        self.assertIsNone(first["fee_error"])
        self.assertEqual(first["fee_multiplier"], 2)
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 2)

    def test_paginated_cleared_override_restores_series_schedule(self):
        def fetch(path, params=None, *, signed=True):
            if path.startswith("/series/"):
                return {"series": {"ticker": "KXHIGHCHI", "fee_type": "quadratic", "fee_multiplier": 0.5}}
            clear = bool(params.get("cursor"))
            row = {"event_ticker": "KXHIGHCHI-26SEP06", "series_ticker": "KXHIGHCHI", "scheduled_ts": "2021-01-01T00:00:00Z" if clear else "2020-01-01T00:00:00Z", "fee_type_override": None if clear else "flat", "fee_multiplier_override": None if clear else 3}
            return {"event_fee_changes": [row], "cursor": "" if clear else "next"}
        with patch.object(self.client, "_get", side_effect=fetch) as mocked:
            result = self.client.get_weather_fee_metadata("KXHIGHCHI-26SEP06-B82.5")
        self.assertEqual(result["fee_multiplier"], 0.5)
        self.assertIsNone(result["fee_error"])
        self.assertEqual(mocked.call_count, 3)

    def test_failed_fee_lookup_is_cached_and_does_not_become_default(self):
        with patch.object(self.client, "_get", side_effect=ValueError("unavailable")) as mocked:
            first = self.client.get_weather_fee_metadata("KXHIGHCHI-26SEP06-B82.5")
            self.client.get_weather_fee_metadata("KXHIGHCHI-26SEP06-B84.5")
        self.assertIn("UNVERIFIED", first["fee_error"])
        self.assertIsNone(first["fee_type"])
        self.assertEqual(mocked.call_count, 1)


if __name__ == "__main__":
    unittest.main()
