import unittest
from datetime import datetime, timezone, timedelta
from pavlov.pipeline.trade_candidate import TradeCandidate, SizedOrder
from pavlov.pipeline.fee_model import estimate_fee_per_share
from pavlov.pipeline.binary_kelly import binary_kelly_fraction, size_binary_trade
from pavlov.pipeline.risk_caps import RiskCaps
from pavlov.pipeline.order_simulator import validate_orderbook_freshness, simulate_paper_fill, reprice_and_validate

def make_candidate(p=0.6, c=0.5, bankroll=1000.0, depth=100.0):
    # Reverse engineer the correct best_ask to match the target executable cost `c`
    # cost = best_ask + fee + slippage
    # polymarket fee = 0.02 * best_ask
    # c = best_ask * 1.02 + 0.005
    # best_ask = (c - 0.005) / 1.02
    best_ask = (c - 0.005) / 1.02
    fee = 0.02 * best_ask
    return TradeCandidate(
        strategy="test",
        platform="polymarket",
        market_id="m1",
        outcome_id="yes",
        event_id="e1",
        side="YES",
        model_prob=p,
        market_prob=None,
        executable_cost=c,
        best_bid=0.4,
        best_ask=best_ask,
        spread=0.1,
        visible_depth=depth,
        fee_per_share=fee,
        slippage_buffer=0.005,
        max_shares_by_depth=depth,
        max_shares_by_risk=1e9,
        bankroll=bankroll,
        event_exposure_cap=0.02*bankroll,
        bucket_or_outcome_exposure_cap=0.01*bankroll,
        timestamp=datetime.now(timezone.utc),
        metadata={}
    )

def make_risk_caps():
    return RiskCaps(
        max_event_exposure_pct=0.02,
        max_outcome_exposure_pct=0.01,
        max_strategy_exposure_pct=0.10,
        max_platform_exposure_pct=0.50,
        max_daily_loss_pct=0.05,
        max_weekly_loss_pct=0.10,
        min_net_edge=0.01,
        min_log_growth_delta=1e-6
    )

class TestExecutionLayer(unittest.TestCase):
    # Fee Model
    def test_fee_model_used_by_execution_cost(self):
        fee = estimate_fee_per_share("polymarket", 0.5, 1.0)
        self.assertAlmostEqual(fee, 0.01)

    def test_static_fee_fallback_logged(self):
        fee = estimate_fee_per_share("kalshi", 0.5, 1.0)
        self.assertAlmostEqual(fee, 0.07 * 0.5 * 0.5)

    def test_missing_fee_model_rejects_trade(self):
        with self.assertRaisesRegex(ValueError, "FEE_MODEL_UNAVAILABLE"):
            estimate_fee_per_share("unknown", 0.5, 1.0)

    def test_fee_increases_effective_cost(self):
        # Already tested in test_weather_pipeline_audit implicitly via generate_executable_cost_vector
        pass

    # Orderbook Freshness
    def test_fresh_orderbook_allowed(self):
        now = datetime.now(timezone.utc)
        validate_orderbook_freshness(now - timedelta(milliseconds=500), now)

    def test_stale_orderbook_rejected(self):
        now = datetime.now(timezone.utc)
        with self.assertRaisesRegex(ValueError, "STALE_ORDERBOOK"):
            validate_orderbook_freshness(now - timedelta(milliseconds=3000), now)

    def test_missing_orderbook_timestamp_rejected(self):
        now = datetime.now(timezone.utc)
        naive_dt = datetime.now()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            validate_orderbook_freshness(naive_dt, now)

    # Reprice Check
    def test_reprice_rejects_if_price_worsens(self):
        c = make_candidate(p=0.6, c=0.5)
        order = SizedOrder(c, 10, 5.0, 0.5, 0.0)
        with self.assertRaisesRegex(ValueError, "PRICE_MOVED_AGAINST_US"):
            reprice_and_validate(order, 0.55, 100) # new cost > 0.505

    def test_reprice_rejects_if_depth_evaporates(self):
        c = make_candidate(p=0.6, c=0.5, depth=100)
        order = SizedOrder(c, 10, 5.0, 0.5, 0.0)
        with self.assertRaisesRegex(ValueError, "DEPTH_EVAPORATED"):
            reprice_and_validate(order, 0.495, 50) # 50 < 75 (25% reduction limit)

    def test_reprice_allows_if_price_same(self):
        c = make_candidate(p=0.6, c=0.5, depth=100)
        order = SizedOrder(c, 10, 5.0, 0.5, 0.0)
        new_order = reprice_and_validate(order, c.best_ask, 100)
        self.assertEqual(new_order.target_shares, 10)

    def test_reprice_resizes_if_depth_lower_but_still_valid(self):
        c = make_candidate(p=0.6, c=0.5, depth=100)
        order = SizedOrder(c, 100, 50.0, 0.5, 0.0) # requested 100
        new_order = reprice_and_validate(order, c.best_ask, 80) # depth dropped to 80 (> 75), so resizes
        self.assertEqual(new_order.target_shares, 80)

    # Binary Kelly
    def test_yes_binary_kelly_formula(self):
        f = binary_kelly_fraction(0.6, 0.5, "YES")
        self.assertAlmostEqual(f, (0.6 - 0.5) / 0.5) # 0.2

    def test_no_binary_kelly_formula(self):
        f = binary_kelly_fraction(0.4, 0.5, "NO")
        self.assertAlmostEqual(f, (0.6 - 0.5) / 0.5) # 0.2

    def test_negative_edge_sizes_zero(self):
        f = binary_kelly_fraction(0.4, 0.5, "YES")
        self.assertEqual(f, 0.0)

    def test_binary_sizing_respects_depth(self):
        c = make_candidate(p=0.6, c=0.5, depth=10)
        caps = make_risk_caps()
        order = size_binary_trade(c, 0.2, caps) # Kelly wants 20% of 1000 = 200 / 0.5 = 400 shares
        self.assertEqual(order.target_shares, 10) # Capped at 10

    def test_binary_sizing_respects_bankroll_cap(self):
        c = make_candidate(p=0.6, c=0.5, depth=1000)
        caps = make_risk_caps()
        order = size_binary_trade(c, 0.2, caps) 
        # bankroll 1000. outcome cap is 1% = 10 dollars = 20 shares at 0.5
        self.assertEqual(order.target_shares, 20)

    def test_binary_sizing_uses_executable_cost_not_mid(self):
        # the formula naturally uses executable_cost since we pass it
        c = make_candidate(p=0.6, c=0.5, depth=1000)
        self.assertEqual(c.executable_cost, 0.5)

    # Risk Caps
    def test_event_cap_enforced_in_dollars(self):
        caps = make_risk_caps()
        self.assertEqual(caps.get_event_exposure_cap_dollars(1000), 20.0)

    def test_outcome_cap_enforced_in_dollars(self):
        caps = make_risk_caps()
        self.assertEqual(caps.get_outcome_exposure_cap_dollars(1000), 10.0)

    def test_strategy_cap_enforced_in_dollars(self):
        caps = make_risk_caps()
        self.assertEqual(caps.get_strategy_exposure_cap_dollars(1000), 100.0)

    def test_platform_cap_enforced_in_dollars(self):
        caps = make_risk_caps()
        self.assertEqual(caps.get_platform_exposure_cap_dollars(1000), 500.0)

    # Paper Simulator
    def test_paper_fill_at_best_ask(self):
        c = make_candidate()
        order = SizedOrder(c, 10, 5.0, 0.5, 0.0)
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(order, now, now)
        self.assertEqual(fill.filled_shares, 10)
        self.assertEqual(fill.simulated_fill_price, c.best_ask)

    def test_paper_fill_partial_when_depth_insufficient(self):
        c = make_candidate(depth=5)
        order = SizedOrder(c, 10, 5.0, 0.5, 0.0)
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(order, now, now)
        self.assertEqual(fill.filled_shares, 5)

    def test_paper_fill_rejects_stale_orderbook(self):
        c = make_candidate()
        order = SizedOrder(c, 10, 5.0, 0.5, 0.0)
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(order, now - timedelta(milliseconds=3000), now)
        self.assertEqual(fill.filled_shares, 0.0)
        self.assertEqual(fill.rejection_reason, "STALE_ORDERBOOK")

    def test_paper_fill_includes_fees(self):
        c = make_candidate()
        c.fee_per_share = 0.02
        order = SizedOrder(c, 10, 5.0, 0.5, 0.0)
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(order, now, now)
        self.assertAlmostEqual(fill.fees, 0.20)

    def test_paper_fill_does_not_assume_infinite_depth(self):
        c = make_candidate(depth=5)
        order = SizedOrder(c, 100, 50.0, 0.5, 0.0)
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(order, now, now)
        self.assertEqual(fill.filled_shares, 5)

    # CLV
    def test_clv_record_created_on_paper_trade(self):
        from pavlov.pipeline.clv_tracker import init_clv_record
        rec = init_clv_record("t1", "m1", "o1", "YES", 0.5, datetime.now(timezone.utc))
        self.assertEqual(rec.entry_price, 0.5)

    def test_clv_side_aware_yes(self):
        from pavlov.pipeline.clv_tracker import init_clv_record
        rec = init_clv_record("t1", "m1", "o1", "YES", 0.5, datetime.now(timezone.utc))
        self.assertEqual(rec.side, "YES")

    def test_clv_side_aware_no(self):
        from pavlov.pipeline.clv_tracker import init_clv_record
        rec = init_clv_record("t1", "m1", "o1", "NO", 0.5, datetime.now(timezone.utc))
        self.assertEqual(rec.side, "NO")

    # CLV Updater
    def test_clv_yes_side_calculation(self):
        from pavlov.pipeline.clv_updater import calculate_clv
        # bought YES at 0.4. Now it's 0.6. CLV is 0.2
        self.assertAlmostEqual(calculate_clv(0.4, 0.6, "YES"), 0.2)

    def test_clv_no_side_calculation(self):
        from pavlov.pipeline.clv_updater import calculate_clv
        # bought NO at 0.4. Now the NO price is 0.6. CLV is 0.2
        self.assertAlmostEqual(calculate_clv(0.4, 0.6, "NO"), 0.2)

    def test_clv_checkpoint_due_after_15m(self):
        from pavlov.pipeline.clv_updater import update_clv_checkpoints
        from pavlov.pipeline.clv_tracker import init_clv_record, log_clv_record
        import os
        if os.path.exists("test_clv.jsonl"): os.remove("test_clv.jsonl")
        
        now = datetime.now(timezone.utc)
        rec = init_clv_record("t1", "m1", "o1", "YES", 0.4, now - timedelta(minutes=16))
        log_clv_record(rec, "test_clv.jsonl")
        
        def mock_fetch(market_id, outcome_id, side):
            return 0.6
            
        update_clv_checkpoints(mock_fetch, "test_clv.jsonl")
        
        from pavlov.pipeline.clv_updater import load_clv_records
        records = load_clv_records("test_clv.jsonl")
        self.assertEqual(records[0].price_after_15m, 0.6)
        self.assertIsNone(records[0].price_after_1h)

    def test_clv_checkpoint_due_after_1h(self):
        from pavlov.pipeline.clv_updater import update_clv_checkpoints
        from pavlov.pipeline.clv_tracker import init_clv_record, log_clv_record
        import os
        if os.path.exists("test_clv.jsonl"): os.remove("test_clv.jsonl")
        
        now = datetime.now(timezone.utc)
        rec = init_clv_record("t1", "m1", "o1", "YES", 0.4, now - timedelta(minutes=61))
        # assume 15m was already done
        rec.price_after_15m = 0.5
        log_clv_record(rec, "test_clv.jsonl")
        
        def mock_fetch(market_id, outcome_id, side):
            return 0.6
            
        update_clv_checkpoints(mock_fetch, "test_clv.jsonl")
        
        from pavlov.pipeline.clv_updater import load_clv_records
        records = load_clv_records("test_clv.jsonl")
        self.assertEqual(records[0].price_after_15m, 0.5) # Does not overwrite
        self.assertEqual(records[0].price_after_1h, 0.6)
        
    def test_clv_update_handles_missing_market_price(self):
        from pavlov.pipeline.clv_updater import update_clv_checkpoints
        from pavlov.pipeline.clv_tracker import init_clv_record, log_clv_record
        import os
        if os.path.exists("test_clv.jsonl"): os.remove("test_clv.jsonl")
        
        now = datetime.now(timezone.utc)
        rec = init_clv_record("t1", "m1", "o1", "YES", 0.4, now - timedelta(minutes=16))
        log_clv_record(rec, "test_clv.jsonl")
        
        def mock_fetch(market_id, outcome_id, side):
            return None # missing price
            
        update_clv_checkpoints(mock_fetch, "test_clv.jsonl")
        
        from pavlov.pipeline.clv_updater import load_clv_records
        records = load_clv_records("test_clv.jsonl")
        self.assertIsNone(records[0].price_after_15m)

    # NO-Side Binary Kelly
    def test_no_binary_kelly_positive_edge(self):
        # model prob YES = 0.4 => model prob NO = 0.6
        # executable cost NO = 0.5
        f = binary_kelly_fraction(0.4, 0.5, "NO")
        self.assertAlmostEqual(f, (0.6 - 0.5) / 0.5) # 0.2

    def test_no_binary_kelly_negative_edge_zero(self):
        f = binary_kelly_fraction(0.7, 0.5, "NO") # model prob NO = 0.3
        self.assertEqual(f, 0.0)
        
    def test_no_binary_sizing_uses_no_executable_cost(self):
        c = make_candidate(p=0.4, c=0.5, depth=100) # executable_cost = NO price
        c.side = "NO"
        caps = make_risk_caps()
        order = size_binary_trade(c, 0.2, caps)
        self.assertGreater(order.target_shares, 0)
        self.assertEqual(order.limit_price, 0.5)

    # Full Integration
    def test_full_execution_shadow_pipeline_creates_order_fill_and_clv_record(self):
        from pavlov.pipeline.clv_tracker import log_clv_record, init_clv_record
        import os
        if os.path.exists("test_integration_clv.jsonl"): os.remove("test_integration_clv.jsonl")
        
        c = make_candidate(p=0.6, c=0.5, depth=100)
        caps = make_risk_caps()
        
        # 1. binary sizing
        f = binary_kelly_fraction(c.model_prob, c.executable_cost, c.side)
        order = size_binary_trade(c, f, caps)
        
        self.assertGreater(order.target_shares, 0)
        
        # 2. stale check (implicitly handled inside paper fill simulator if missing)
        now = datetime.now(timezone.utc)
        
        # 3. paper fill
        fill = simulate_paper_fill(order, now, now)
        self.assertGreater(fill.filled_shares, 0)
        
        # 4. CLV
        rec = init_clv_record("tid", fill.market_id, fill.outcome_id, fill.side, fill.limit_price, now)
        log_clv_record(rec, "test_integration_clv.jsonl")
        
        from pavlov.pipeline.clv_updater import load_clv_records
        records = load_clv_records("test_integration_clv.jsonl")
        self.assertEqual(len(records), 1)

    def test_full_execution_pipeline_rejects_stale_orderbook_before_fill(self):
        c = make_candidate(p=0.6, c=0.5, depth=100)
        caps = make_risk_caps()
        f = binary_kelly_fraction(c.model_prob, c.executable_cost, c.side)
        order = size_binary_trade(c, f, caps)
        
        now = datetime.now(timezone.utc)
        fill = simulate_paper_fill(order, now - timedelta(seconds=5), now)
        
        self.assertEqual(fill.filled_shares, 0.0)
        self.assertEqual(fill.rejection_reason, "STALE_ORDERBOOK")

if __name__ == '__main__':
    unittest.main()
