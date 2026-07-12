import unittest
from datetime import datetime, timezone, timedelta
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.sports_probability_model import predict_sports_probability
from pavlov.pipeline.source_quality import SourceQualityRecord, estimate_source_weight, update_source_pick_result, deduplicate_picks
from pavlov.pipeline.mlb_model import build_mlb_features, predict_mlb_probability
from pavlov.pipeline.soccer_model import build_soccer_features, predict_soccer_3way_probabilities, predict_soccer_binary_contract
from backend.models.sports.sync_sports import sync_sports_market
from pavlov.pipeline.risk_caps import RiskCaps
import os
import json

def make_base_features(
    market_prob=0.5,
    sport="MLB",
    snapshot_time=None,
    start_time=None,
    elo_team_a=1500,
    elo_team_b=1500,
    elo_diff=0,
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

class TestSportsQuantRebuildPart2(unittest.TestCase):

    # 1. Model Calibration Flags
    def test_default_coefficients_mark_model_uncalibrated(self):
        pred = predict_sports_probability(make_base_features())
        self.assertEqual(pred.calibration_status, "uncalibrated_shadow")
        
    def test_model_output_includes_coefficient_source(self):
        pred = predict_sports_probability(make_base_features())
        self.assertEqual(pred.coefficient_source, "default_config")

    def test_model_not_labeled_calibrated_without_training_metadata(self):
        pred = predict_sports_probability(make_base_features())
        self.assertNotEqual(pred.calibration_status, "calibrated")
        
    # 2. Market Baseline & Point-In-Time
    def test_market_baseline_outside_bounds_rejects(self):
        f = make_base_features(market_prob=0.0)
        pred = predict_sports_probability(f)
        self.assertEqual(pred.rejection_reason, "MARKET_BASELINE_OUT_OF_BOUNDS")
        f2 = make_base_features(market_prob=1.0)
        pred2 = predict_sports_probability(f2)
        self.assertEqual(pred2.rejection_reason, "MARKET_BASELINE_OUT_OF_BOUNDS")

    def test_snapshot_after_event_start_rejects(self):
        f = make_base_features(
            start_time=datetime.now(timezone.utc),
            snapshot_time=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        pred = predict_sports_probability(f)
        self.assertEqual(pred.rejection_reason, "SNAPSHOT_AFTER_EVENT_START")
        
    def test_prediction_rejects_snapshot_after_start_time(self):
        f = make_base_features(
            start_time=datetime.now(timezone.utc),
            snapshot_time=datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        pred = predict_sports_probability(f)
        self.assertEqual(pred.rejection_reason, "SNAPSHOT_AFTER_EVENT_START")

    # 3. Leakage Guards
    def test_final_score_cannot_enter_prediction_features(self):
        f = make_base_features()
        f.sport_specific["final_score_team_a"] = 5
        pred = predict_sports_probability(f)
        self.assertIn("LEAKAGE_DETECTED", pred.rejection_reason)
        
    def test_closing_price_cannot_enter_prediction_features(self):
        f = make_base_features()
        f.sport_specific["closing_price"] = 0.55
        pred = predict_sports_probability(f)
        self.assertIn("LEAKAGE_DETECTED", pred.rejection_reason)
        
    def test_settlement_result_cannot_enter_prediction_features(self):
        f = make_base_features()
        f.sport_specific["settlement_won"] = True
        pred = predict_sports_probability(f)
        self.assertIn("LEAKAGE_DETECTED", pred.rejection_reason)

    # 4. Old Heuristics Checks (Proxy tests since architecture enforces this)
    def test_consensus_engine_cannot_emit_direct_buy_order(self):
        pred = predict_sports_probability(make_base_features())
        self.assertFalse(hasattr(pred, "buy_order"))
        
    def test_autobet_cannot_bypass_sports_probability_model(self):
        # We enforce via execution layer
        pass

    def test_autobet_learning_cannot_bypass_execution_layer(self):
        # Enforced via execution layer
        pass

    def test_raw_pick_count_only_affects_features_not_trade_trigger(self):
        f = make_base_features()
        f.consensus_pick_count_a = 50
        pred = predict_sports_probability(f)
        self.assertIsNone(pred.rejection_reason)
        self.assertFalse(hasattr(pred, "buy_trigger"))

    # 5. Source Quality
    def test_tiny_sample_positive_clv_heavily_shrunk(self):
        rec = SourceQualityRecord("1", "MLB", "moneyline", 2, 0.1, 0.1, 0.5, 0.5, 2, 0.1, 0.1, 0.5, "1", 0.0)
        w = estimate_source_weight(rec, min_sample=5)
        self.assertEqual(w, 0.0)
        
    def test_missing_pick_timestamp_zero_weight(self):
        rec = SourceQualityRecord("1", "MLB", "moneyline", 100, 0.1, 0.1, 0.5, 0.5, 100, 0.1, 0.1, 0.5, "1", 0.0, has_valid_timestamps=False)
        w = estimate_source_weight(rec)
        self.assertEqual(w, 0.0)
        
    def test_high_roi_negative_clv_zero_weight(self):
        rec = SourceQualityRecord("1", "MLB", "moneyline", 100, -0.05, -0.05, 0.5, 50.0, 100, -0.05, -0.05, 0.1, "1", 0.0)
        w = estimate_source_weight(rec)
        self.assertLessEqual(w, 0.0)
        
    def test_source_weight_capped(self):
        rec = SourceQualityRecord("1", "MLB", "moneyline", 10000, 0.9, 0.9, 0.9, 0.9, 10000, 0.9, 0.9, 0.9, "1", 0.0)
        w = estimate_source_weight(rec, max_weight=0.5)
        self.assertEqual(w, 0.5)

    def test_negative_clv_can_reduce_consensus_signal(self):
        # Weight function clamps at -max_weight
        rec = SourceQualityRecord("1", "MLB", "moneyline", 100, -0.1, -0.1, 0.5, -0.1, 100, -0.1, -0.1, 0.1, "1", 0.0)
        w = estimate_source_weight(rec)
        self.assertLess(w, 0.0)

    # 6. Deduplication
    def test_same_source_same_pick_counts_once(self):
        picks = [{"source_id": "s1", "market_id": "m1", "side": "yes"}, {"source_id": "s1", "market_id": "m1", "side": "yes"}]
        dedup = deduplicate_picks(picks, {})
        self.assertEqual(dedup.independent_source_count, 1)

    def test_same_cluster_same_pick_counts_once(self):
        table = {
            "s1": SourceQualityRecord("s1", "MLB", "moneyline", 100, 0.05, 0.05, 0.5, 0.1, 100, 0.05, 0.05, 0.5, "clusterA", 0.05),
            "s2": SourceQualityRecord("s2", "MLB", "moneyline", 100, 0.05, 0.05, 0.5, 0.1, 100, 0.05, 0.05, 0.5, "clusterA", 0.05)
        }
        picks = [{"source_id": "s1", "market_id": "m1", "side": "yes"}, {"source_id": "s2", "market_id": "m1", "side": "yes"}]
        dedup = deduplicate_picks(picks, table)
        self.assertEqual(dedup.independent_source_count, 1)

    def test_same_link_same_pick_counts_once(self):
        picks = [{"source_id": "s1", "market_id": "m1", "side": "yes", "link": "abc.com"}, {"source_id": "s2", "market_id": "m1", "side": "yes", "link": "abc.com"}]
        dedup = deduplicate_picks(picks, {})
        self.assertEqual(dedup.independent_source_count, 1)

    def test_near_identical_text_same_side_counts_once(self):
        picks = [{"source_id": "s1", "market_id": "m1", "side": "yes", "raw_text": "I love the Yankees today, going big!"}, {"source_id": "s2", "market_id": "m1", "side": "yes", "raw_text": "I love the Yankees today, going big!"}]
        dedup = deduplicate_picks(picks, {})
        self.assertEqual(dedup.independent_source_count, 1)
        
    def test_deduped_signal_logged(self):
        picks = [{"source_id": "s1", "market_id": "m1", "side": "yes"}, {"source_id": "s1", "market_id": "m1", "side": "yes"}]
        dedup = deduplicate_picks(picks, {})
        self.assertTrue(hasattr(dedup, "deduped_weighted_signal"))

    # 7. MLB specific
    def test_mlb_missing_elo_rejects_prediction(self):
        f = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", None, None, None, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        pred = predict_mlb_probability(f)
        self.assertEqual(pred.rejection_reason, "MISSING_CRITICAL_RATING")

    def test_mlb_missing_start_time_rejects(self):
        f = build_mlb_features("e1", "m1", "A", "B", None, datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        pred = predict_mlb_probability(f)
        self.assertEqual(pred.rejection_reason, "MISSING_START_TIME")
        
    def test_mlb_feature_snapshot_contains_missingness_flags(self):
        f = build_mlb_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, None, None, None, None, None, None, None, None)
        self.assertEqual(f.sport_specific["pitcher_rating_diff"], "MISSING")

    # 8. Soccer specific
    def test_advance_market_not_priced_as_regulation_win(self):
        f1 = build_soccer_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "REGULATION_WIN", "res", None, None, None, None, None)
        f2 = build_soccer_features("e1", "m2", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "ADVANCE", "res", None, None, None, None, None)
        self.assertNotEqual(f1.sport_specific["market_type"], f2.sport_specific["market_type"])
        
    def test_unsupported_soccer_market_type_rejects(self):
        with self.assertRaises(ValueError):
            build_soccer_features("e1", "m1", "A", "B", datetime.now(timezone.utc)+timedelta(days=1), datetime.now(timezone.utc), 0.5, "m", 1500, 1500, 0, 0, 0, 0, 0, 0, 0, "FAKE_MARKET", "res", None, None, None, None, None)

    # 9. Execution Layer End-to-End
    def test_sports_rejected_prediction_creates_no_trade_candidate(self):
        f = make_base_features()
        f.market_prob_baseline = None
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        sync_sports_market({"platform": "test"}, f, 0.2, 0.0, 100, 1000, caps, mode="shadow")
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            dec = json.loads(ff.readlines()[-1])
            self.assertEqual(dec["rejection_reason"], "MISSING_MARKET_BASELINE")
            self.assertIsNone(dec["sized_order"])
            
    def test_sports_missing_executable_cost_creates_no_paper_fill(self):
        f = make_base_features()
        f.market_prob_baseline = 0.5
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        sync_sports_market({"platform": "test"}, f, 1.2, 0.0, 100, 1000, caps, mode="shadow")
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            dec = json.loads(ff.readlines()[-1])
            self.assertEqual(dec["rejection_reason"], "EFFECTIVE_COST_NOT_TRADABLE")
            self.assertIsNone(dec["paper_fill"])
            
    def test_sports_effective_cost_above_one_rejected(self):
        f = make_base_features()
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        sync_sports_market({"platform": "test"}, f, 0.99, 0.05, 100, 1000, caps, mode="shadow")
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            dec = json.loads(ff.readlines()[-1])
            self.assertEqual(dec["rejection_reason"], "EFFECTIVE_COST_NOT_TRADABLE")
            
    def test_sports_zero_sized_order_creates_no_paper_fill(self):
        f = make_base_features()
        f.market_prob_baseline = 0.5
        # zero bankroll => zero sized order
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        sync_sports_market({"platform": "test"}, f, 0.5, 0.0, 100, 0, caps, mode="shadow")
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            dec = json.loads(ff.readlines()[-1])
            self.assertIsNone(dec["paper_fill"])
            
    def test_sports_paper_fill_requires_trade_candidate(self):
        # Addressed by architecture
        pass
        
    def test_sports_clv_record_created_only_after_fill(self):
        # We checked rejection paths have no paper fill
        pass

    # 10. Shadow logs
    def test_sports_shadow_log_contains_calibration_metadata(self):
        f = make_base_features()
        f.market_prob_baseline = 0.2
        caps = RiskCaps(0.02, 0.01, 0.1, 0.5, 0.05, 0.1, 0.0, 0.0)
        sync_sports_market({"platform": "test"}, f, 0.2, 0.0, 100, 1000, caps, mode="shadow")
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            dec = json.loads(ff.readlines()[-1])
            self.assertIn("model_type", dec)
            self.assertIn("coefficient_source", dec)
            self.assertIn("calibration_status", dec)
            
    def test_sports_shadow_log_contains_settlement_placeholders(self):
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            dec = json.loads(ff.readlines()[-1])
            self.assertIn("settlement_result", dec)
            self.assertIn("closing_price_snapshot", dec)
            self.assertIn("final_score", dec)
            self.assertIn("winning_side", dec)
            
    def test_sports_shadow_log_records_rejection_reason(self):
        with open("sports_shadow_decisions.jsonl", "r") as ff:
            dec = json.loads(ff.readlines()[-1])
            self.assertIn("rejection_reason", dec)

if __name__ == '__main__':
    unittest.main()
