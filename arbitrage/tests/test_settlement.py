from __future__ import annotations

import unittest

from arbitrage.backend.settlement import parse_kalshi_result, parse_polymarket_result


class SettlementParserTests(unittest.TestCase):
    def test_kalshi_requires_final_binary_result(self):
        self.assertEqual(parse_kalshi_result({"market": {"result": "yes"}}), "yes")
        self.assertIsNone(parse_kalshi_result({"market": {"result": ""}}))

    def test_polymarket_requires_closed_one_hot_result(self):
        payload = {
            "closed": True,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
        }
        self.assertEqual(parse_polymarket_result(payload), "yes")
        payload["closed"] = False
        self.assertIsNone(parse_polymarket_result(payload))


if __name__ == "__main__":
    unittest.main()
