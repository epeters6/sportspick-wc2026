"""Evidence-path orderbook freshness and depth rules."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pavlov.pipeline.execution_cost import generate_executable_cost_vector
from pavlov.pipeline.order_simulator import (
    simulate_paper_fill,
    validate_orderbook_freshness,
)
from pavlov.pipeline.trade_candidate import SizedOrder, TradeCandidate


def _candidate(depth: float = 100.0) -> TradeCandidate:
    now = datetime.now(timezone.utc)
    return TradeCandidate(
        strategy="test",
        platform="polymarket",
        market_id="m1",
        outcome_id="yes_token_abc",
        event_id="e1",
        side="YES",
        model_prob=0.6,
        market_prob=None,
        executable_cost=0.5,
        best_bid=0.4,
        best_ask=0.48,
        spread=0.08,
        visible_depth=depth,
        fee_per_share=0.01,
        slippage_buffer=0.005,
        max_shares_by_depth=depth,
        max_shares_by_risk=1e9,
        bankroll=1000.0,
        event_exposure_cap=20.0,
        bucket_or_outcome_exposure_cap=10.0,
        timestamp=now,
        metadata={},
        received_timestamp=now,
        orderbook_timestamp=now,
    )


def _order(depth: float = 100.0) -> SizedOrder:
    c = _candidate(depth=depth)
    return SizedOrder(c, 10.0, 5.0, 0.5, 0.0)


class TestOrderbookEvidence(unittest.TestCase):
    def test_missing_orderbook_timestamp_rejected(self):
        now = datetime.now(timezone.utc)
        with self.assertRaisesRegex(ValueError, "MISSING_ORDERBOOK_TIMESTAMP"):
            validate_orderbook_freshness(None, now)
        fill = simulate_paper_fill(_order(), None, now)
        self.assertEqual(fill.filled_shares, 0.0)
        self.assertEqual(fill.rejection_reason, "MISSING_ORDERBOOK_TIMESTAMP")

    def test_received_alone_not_enough(self):
        """Exchange orderbook_timestamp missing → reject even if received is fresh."""
        now = datetime.now(timezone.utc)
        with self.assertRaisesRegex(ValueError, "MISSING_ORDERBOOK_TIMESTAMP"):
            validate_orderbook_freshness(None, now, mode="shadow")

    def test_naive_timestamp_rejected(self):
        now = datetime.now(timezone.utc)
        naive = datetime(2026, 7, 21, 12, 0, 0)  # intentionally naive
        with self.assertRaisesRegex(ValueError, "NAIVE_ORDERBOOK_TIMESTAMP"):
            validate_orderbook_freshness(naive, now)
        fill = simulate_paper_fill(_order(), naive, now)
        self.assertEqual(fill.filled_shares, 0.0)
        self.assertEqual(fill.rejection_reason, "NAIVE_ORDERBOOK_TIMESTAMP")

    def test_stale_orderbook_rejected(self):
        now = datetime.now(timezone.utc)
        stale = now - timedelta(milliseconds=3000)
        with self.assertRaisesRegex(ValueError, "STALE_ORDERBOOK"):
            validate_orderbook_freshness(stale, now)
        fill = simulate_paper_fill(_order(), stale, now)
        self.assertEqual(fill.filled_shares, 0.0)
        self.assertEqual(fill.rejection_reason, "STALE_ORDERBOOK")

    def test_fresh_aware_passes(self):
        now = datetime.now(timezone.utc)
        fresh = now - timedelta(milliseconds=200)
        validate_orderbook_freshness(fresh, now)
        fill = simulate_paper_fill(_order(), fresh, now)
        self.assertEqual(fill.filled_shares, 10.0)
        self.assertIsNone(fill.rejection_reason)

    def test_assumed_freshness_rejected_for_fills(self):
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(
            _order(),
            now,
            now,
            mode="shadow",
            allow_assumed_fresh_orderbook_for_shadow=True,
        )
        self.assertEqual(fill.filled_shares, 0.0)
        self.assertEqual(fill.rejection_reason, "ASSUMED_FRESHNESS_INVALID")

    def test_zero_depth_no_fill(self):
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(_order(depth=0.0), now, now)
        self.assertEqual(fill.filled_shares, 0.0)
        self.assertEqual(fill.rejection_reason, "INSUFFICIENT_DEPTH")

    def test_no_assumed_depth_50(self):
        raw_markets = [{"yes_ask": 0.35}]  # no ask_size
        q_exec, depth = generate_executable_cost_vector(raw_markets, "polymarket")
        self.assertGreater(q_exec[0], 0.35)
        self.assertLess(q_exec[0], 1.0)
        self.assertEqual(depth[0], 0.0)
        self.assertNotEqual(depth[0], 50.0)


if __name__ == "__main__":
    unittest.main()
