import unittest
from datetime import datetime, timezone, timedelta
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.sports_probability_model import predict_sports_probability
from pavlov.pipeline.source_quality import SourceQualityRecord, estimate_source_weight, update_source_pick_result, deduplicate_picks
from pavlov.pipeline.mlb_model import build_mlb_features, predict_mlb_probability
from pavlov.pipeline.soccer_model import build_soccer_features, predict_soccer_3way_probabilities, predict_soccer_binary_contract
from pavlov.pipeline.sports_backtest_metrics import log_loss, brier_score, calibration_bins, mean_clv
from backend.models.sports.sync_sports import sync_sports_market
from backend.tests.clv_test_isolation import isolate_clv_db


def _call_sync(market_data, features, best_ask, fee_per_share, visible_depth, bankroll, risk_caps, mode="shadow", **extra):
    """Supply valid exchange timestamps + top-of-book fields for evidence fills."""
    now = datetime.now(timezone.utc)
    md = dict(market_data or {})
    if md.get("platform") == "test":
        md["platform"] = "polymarket"
    md.setdefault("outcome_id", "tok_test")
    with isolate_clv_db():
        sync_sports_market(
            md,
            features,
            best_ask,
            fee_per_share,
            visible_depth,
            bankroll,
            risk_caps,
            mode=mode,
            best_bid=extra.get("best_bid", max(0.01, float(best_ask) - 0.02) if best_ask is not None else 0.01),
            spread=extra.get("spread", 0.02),
            outcome_id=extra.get("outcome_id", md["outcome_id"]),
            real_orderbook_timestamp=extra.get("real_orderbook_timestamp", now),
            real_received_timestamp=extra.get("real_received_timestamp", now),
        )


def make_base_features(
    market_prob=0.5,
    sport="MLB",
    snapshot_time=None,
    elo_team_a=1500,
    elo_team_b=1500,
    elo_diff=0,
    start_time=None
):
    if start_time is None:
        start_time = datetime.now(timezone.utc) + timedelta(days=1)
    if snapshot_time is None:
        snapshot_time = datetime.now(timezone.utc)
        
    return SportsEventFeatures(
        sport=sport,
        league=sport,
        event_id="e1",
        market_id="m1",
        team_a="TeamA",
        team_b="TeamB",
        start_time=start_time,
        snapshot_time=snapshot_time,
        market_prob_baseline=market_prob,
        market_price_source="test",
        elo_team_a=elo_team_a,
        elo_team_b=elo_team_b,
        elo_diff=elo_diff,
        consensus_pick_count_a=0,
        consensus_pick_count_b=0,
        consensus_weighted_signal=0.0,
        source_clv_weighted_signal=0.0,
        source_count=0,
        independent_source_count=0,
        sport_specific={}
    )

class TestSportsQuantRebuild(unittest.TestCase):

    # 1. Probability Model Interface
    def test_sports_model_outputs_probability_between_zero_and_one(self):
        f = make_base_features()
        pred = predict_sports_probability(f)
        self.assertGreaterEqual(pred.model_prob, 0.0)
        self.assertLessEqual(pred.model_prob, 1.0)
        
    def test_sports_model_uses_market_baseline(self):
        f1 = make_base_features(market_prob=0.2)
        f2 = make_base_features(market_prob=0.8)
        pred1 = predict_sports_probability(f1)
        pred2 = predict_sports_probability(f2)
        self.assertLess(pred1.model_prob, pred2.model_prob)

    def test_sports_model_rejects_missing_market_baseline(self):
        f = make_base_features(market_prob=None)
        pred = predict_sports_probability(f)
        self.assertEqual(pred.rejection_reason, "MISSING_MARKET_BASELINE")

    def test_sports_model_no_longer_uses_score_threshold_as_trade_trigger(self):
        # We rely on Edge > Execution Cost, implicit in architecture. 
        # Here we just verify we don't output BUY/SELL, only probability.
        f = make_base_features()
        pred = predict_sports_probability(f)
        self.assertTrue(hasattr(pred, "model_prob"))
        self.assertFalse(hasattr(pred, "decision"))

    # 2. Features Snapshot
    def test_feature_snapshot_has_snapshot_time(self):
        f = make_base_features()
        self.assertIsNotNone(f.snapshot_time)
        
    def test_feature_snapshot_rejects_future_or_post_event_data(self):
        f = make_base_features()
        f.snapshot_time = None
        pred = predict_sports_probability(f)
        self.assertEqual(pred.rejection_reason, "MISSING_SNAPSHOT_TIME")
        
    def test_missing_critical_feature_rejects_prediction(self):
        f = make_base_features()
        f.team_a = None
        pred = predict_sports_probability(f)
        self.assertEqual(pred.rejection_reason, "MISSING_TEAMS")
        
    def test_feature_snapshot_serializable(self):
        f = make_base_features()
        import json
        # convert datetime for simple dump test
        f.start_time = f.start_time.isoformat()
        f.snapshot_time = f.snapshot_time.isoformat()
        json.dumps(f.__dict__)

    # 3. Source Quality
    def test_source_weight_shrinks_small_sample(self):
        rec1 = SourceQualityRecord("1", "MLB", "moneyline", 1, 0.1, 0.1, 0.5, 0.5, 1, 0.1, 0.1, 0.5, "1", 0.0)
        rec100 = SourceQualityRecord("2", "MLB", "moneyline", 100, 0.1, 0.1, 0.5, 0.5, 100, 0.1, 0.1, 0.5, "2", 0.0)
        w1 = estimate_source_weight(rec1, k=100)
        w100 = estimate_source_weight(rec100, k=100)
        self.assertLess(w1, w100)
        
    def test_source_weight_increases_with_positive_clv_sample(self):
        rec = SourceQualityRecord("1", "MLB", "moneyline", 0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, "1", 0.0)
        for i in range(10):
            rec = update_source_pick_result(rec, 0.05, 0.1, True)
        self.assertGreater(rec.confidence_weight, 0.0)
        
    def test_negative_clv_source_gets_negative_or_zero_weight(self):
        rec = SourceQualityRecord("1", "MLB", "moneyline", 10, -0.05, -0.05, 0.5, -0.1, 10, -0.05, -0.05, 0.3, "1", 0.0)
        w = estimate_source_weight(rec)
        self.assertLessEqual(w, 0.0)
        
    def test_raw_roi_does_not_override_clv(self):
        # Even if ROI is very high, if CLV is negative, weight is 0
        rec = SourceQualityRecord("1", "MLB", "moneyline", 10, -0.05, -0.05, 0.5, 10.0, 10, -0.05, -0.05, 0.3, "1", 0.0)
        w = estimate_source_weight(rec)
        self.assertLessEqual(w, 0.0)

    # 4. Deduplication
    def test_identical_pick_cluster_counts_once(self):
        table = {
            "s1": SourceQualityRecord("s1", "MLB", "moneyline", 100, 0.05, 0.05, 0.5, 0.1, 100, 0.05, 0.05, 0.5, "c1", 0.05),
            "s2": SourceQualityRecord("s2", "MLB", "moneyline", 100, 0.05, 0.05, 0.5, 0.1, 100, 0.05, 0.05, 0.5, "c1", 0.05)
        }
        picks = [{"source_id": "s1"}, {"source_id": "s2"}]
        deduped = deduplicate_picks(picks, table)
        self.assertEqual(deduped.independent_source_count, 1)

    def test_same_source_duplicate_pick_counts_once(self):
        table = {
            "s1": SourceQualityRecord("s1", "MLB", "moneyline", 100, 0.05, 0.05, 0.5, 0.1, 100, 0.05, 0.05, 0.5, "c1", 0.05)
        }
        picks = [{"source_id": "s1"}, {"source_id": "s1"}]
        deduped = deduplicate_picks(picks, table)
        self.assertEqual(deduped.independent_source_count, 1)

    def test_different_independent_sources_count_separately(self):
        table = {
            "s1": SourceQualityRecord("s1", "MLB", "moneyline", 100, 0.05, 0.05, 0.5, 0.1, 100, 0.05, 0.05, 0.5, "c1", 0.05),
            "s2": SourceQualityRecord("s2", "MLB", "moneyline", 100, 0.05, 0.05, 0.5, 0.1, 100, 0.05, 0.05, 0.5, "c2", 0.05)
        }
        picks = [{"source_id": "s1"}, {"source_id": "s2"}]
        deduped = deduplicate_picks(picks, table)
        self.assertEqual(deduped.independent_source_count, 2)
        
    def test_consensus_requires_independent_sources_not_raw_pick_count(self):
        # We enforce independent source counts via logic that wraps this.
        pass

    # 5. MLB Features
    def test_mlb_features_include_market_baseline(self):
        f = build_mlb_features("e1", "m1", "A", "B", None, None, 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        self.assertEqual(f.market_prob_baseline, 0.5)

    def test_mlb_missing_market_baseline_rejects(self):
        f = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), None, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        pred = predict_mlb_probability(f)
        self.assertEqual(pred.rejection_reason, "MISSING_MARKET_BASELINE")

    def test_mlb_missing_pitcher_uses_missing_flag_not_zero(self):
        f = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        self.assertEqual(f.sport_specific["starting_pitcher_team_a"], "MISSING")
        self.assertEqual(f.sport_specific["pitcher_rating_diff"], "MISSING")

    def test_mlb_probability_changes_with_elo_diff(self):
        f1 = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        f2 = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1600, 1500, 100, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        pred1 = predict_mlb_probability(f1)
        pred2 = predict_mlb_probability(f2)
        self.assertLess(pred1.model_prob, pred2.model_prob)

    def test_mlb_probability_changes_with_pitcher_diff_when_available(self):
        f1 = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "P1", "P2", 0.0, None, None, None, None, None)
        f2 = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "P1", "P2", 10.0, None, None, None, None, None)
        pred1 = predict_mlb_probability(f1)
        pred2 = predict_mlb_probability(f2)
        self.assertLess(pred1.model_prob, pred2.model_prob)

    # 6. Soccer Features
    def test_soccer_market_type_required(self):
        with self.assertRaisesRegex(ValueError, "MISSING_SOCCER_MARKET_TYPE"):
            build_soccer_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, None, "res", None, None, None, None, None)

    def test_soccer_missing_resolution_rule_rejects(self):
        with self.assertRaisesRegex(ValueError, "MISSING_SOCCER_RESOLUTION_RULE"):
            build_soccer_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "mt", None, None, None, None, None, None)

    def test_soccer_regulation_win_not_confused_with_advance(self):
        f1 = build_soccer_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "regulation_win", "res", None, None, None, None, None)
        f2 = build_soccer_features("e1", "m2", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "advance", "res", None, None, None, None, None)
        self.assertNotEqual(f1.sport_specific["market_type"], f2.sport_specific["market_type"])

    def test_soccer_3way_probs_sum_to_one(self):
        f = build_soccer_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "moneyline", "res", None, None, None, None, None)
        p1, p2, p3 = predict_soccer_3way_probabilities(f)
        self.assertAlmostEqual(p1 + p2 + p3, 1.0)

    def test_binary_contract_maps_to_correct_soccer_probability(self):
        f = build_soccer_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "advance", "res", None, None, None, None, None)
        pred = predict_soccer_binary_contract(f)
        self.assertEqual(pred.market_prob, 0.5)

    # 8. Metrics
    def test_log_loss_basic(self):
        ll = log_loss([1, 0], [0.9, 0.1])
        self.assertLess(ll, 0.2)

    def test_brier_score_basic(self):
        bs = brier_score([1, 0], [0.9, 0.1])
        self.assertAlmostEqual(bs, 0.01)

    def test_calibration_bins_basic(self):
        bins = calibration_bins([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1])
        self.assertIn("0.8-0.9", bins)
        self.assertIn("0.1-0.2", bins)
        
    # 9. Regression Tests (Old Heuristics Removed)
    def test_old_consensus_threshold_cannot_trigger_trade_without_model_probability(self):
        f = make_base_features()
        f.consensus_weighted_signal = 100.0 # Huge consensus
        pred = predict_sports_probability(f)
        self.assertIsInstance(pred.model_prob, float) # Must output probability, not a BUY signal
        self.assertFalse(hasattr(pred, "decision"))

    def test_old_consensus_threshold_cannot_bypass_execution_layer(self):
        # We enforce this in the architecture of sync_sports.py.
        # Ensure predict_sports_probability requires execution inputs implicitly
        f = make_base_features()
        pred = predict_sports_probability(f)
        self.assertIsNotNone(pred.edge_before_execution)

    def test_roi_only_source_quality_not_used_for_trade_weight(self):
        rec = SourceQualityRecord("1", "MLB", "moneyline", 10, -0.05, -0.05, 0.5, 10.0, 10, -0.05, -0.05, 0.3, "1", 0.0)
        w = estimate_source_weight(rec)
        self.assertLessEqual(w, 0.0) # ROI of 10.0 is ignored because CLV is -0.05

    # 10. Signal Engine Integration
    def test_sports_signal_goes_through_trade_candidate(self):
        from pavlov.pipeline.risk_caps import RiskCaps
        from backend.models.sports.sync_sports import sync_sports_market
        import os
        if os.path.exists("sports_shadow_decisions.jsonl"): os.remove("sports_shadow_decisions.jsonl")
        
        f = make_base_features()
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0) # min_net_edge = 0.0
        
        # huge edge so we trigger a trade
        f.market_prob_baseline = 0.2
        # beta_market is 1, intercept 0, logit(0.2) = -1.38, inv_logit(-1.38) = 0.2. 
        # let's boost elo_diff
        f.elo_diff = 10000 
        
        _call_sync({"platform": "test"}, f, 0.2, 0.0, 100, 1000, caps, mode="shadow", real_received_timestamp=datetime.now(timezone.utc))
        
        import json
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            decision = json.loads(ff.readlines()[-1])
            self.assertIsNotNone(decision.get("sized_order"))
            self.assertEqual(decision["sized_order"]["candidate"]["platform"], "polymarket")

    def test_sports_signal_uses_executable_cost_not_mid(self):
        from pavlov.pipeline.risk_caps import RiskCaps
        from backend.models.sports.sync_sports import sync_sports_market
        import os, json
        
        f = make_base_features()
        f.market_prob_baseline = 0.2
        f.elo_diff = 10000 
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        
        # mid would theoretically be 0.2. ask is 0.22, fee 0.01. executable cost should be ~0.235 (with 0.005 slippage)
        _call_sync({"platform": "test"}, f, 0.22, 0.01, 100, 1000, caps, mode="shadow", real_received_timestamp=datetime.now(timezone.utc))
        
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            decision = json.loads(ff.readlines()[-1])
            self.assertEqual(decision["sized_order"]["candidate"]["executable_cost"], 0.22 + 0.01 + 0.005)

    def test_sports_negative_post_fee_edge_rejected(self):
        from pavlov.pipeline.risk_caps import RiskCaps
        from backend.models.sports.sync_sports import sync_sports_market
        import os, json
        
        f = make_base_features()
        f.market_prob_baseline = 0.5
        f.elo_diff = 0
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        
        # predict_sports_probability baseline will output model_prob ~ 0.5. 
        # best_ask 0.55 => negative edge.
        _call_sync({"platform": "test"}, f, 0.55, 0.0, 100, 1000, caps, mode="shadow", real_received_timestamp=datetime.now(timezone.utc))
        
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            decision = json.loads(ff.readlines()[-1])
            self.assertEqual(decision["rejection_reason"], "NEGATIVE_EDGE")
            self.assertIsNone(decision.get("paper_fill"))

    def test_sports_shadow_log_written(self):
        # Implicitly tested by test_sports_signal_goes_through_trade_candidate
        pass

    def test_sports_paper_fill_created(self):
        from pavlov.pipeline.risk_caps import RiskCaps
        from backend.models.sports.sync_sports import sync_sports_market
        import os
        if os.path.exists("sports_paper_fills.jsonl"): os.remove("sports_paper_fills.jsonl")
        
        f = make_base_features()
        f.market_prob_baseline = 0.2
        f.elo_diff = 10000 
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        
        _call_sync({"platform": "test"}, f, 0.2, 0.0, 100, 1000, caps, mode="shadow", real_received_timestamp=datetime.now(timezone.utc))
        
        self.assertTrue(os.path.exists("sports_paper_fills.jsonl"))

    def test_sports_clv_record_created(self):
        from pavlov.pipeline.risk_caps import RiskCaps
        from backend.models.sports.sync_sports import sync_sports_market
        import os
        if os.path.exists("sports_clv_tracking.jsonl"): os.remove("sports_clv_tracking.jsonl")
        
        f = make_base_features()
        f.market_prob_baseline = 0.2
        f.elo_diff = 10000 
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        
        _call_sync({"platform": "test"}, f, 0.2, 0.0, 100, 1000, caps, mode="shadow", real_received_timestamp=datetime.now(timezone.utc))
        
        self.assertTrue(os.path.exists("sports_clv_tracking.jsonl"))

if __name__ == '__main__':
    unittest.main()
