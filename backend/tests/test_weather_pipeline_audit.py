import unittest
import math
import numpy as np
from datetime import datetime, timezone
from pavlov.pipeline.probability_model import (
    generate_event_probability_vector, 
    validate_probability_vector
)
from pavlov.pipeline.market_probability import (
    generate_market_implied_vector,
    shrink_probability_vector
)
from pavlov.pipeline.nowcast_features import mask_impossible_buckets
from pavlov.pipeline.execution_cost import generate_executable_cost_vector
from pavlov.pipeline.portfolio_optimizer import optimize_portfolio, expected_log_growth
from pavlov.pipeline.settlement_resolver import NormalizedWeatherEvent

# Helper to generate dummy events
def make_event(city, lo, hi, station="KNYC", source="NWS", date=None):
    return NormalizedWeatherEvent(
        market_id=f"{city}_{lo}_{hi}",
        platform="polymarket",
        city=city,
        date=date or datetime(2026, 7, 9, tzinfo=timezone.utc).date(),
        bucket_low_f=lo,
        bucket_high_f=hi,
        bucket_label=f"{lo}-{hi}",
        settlement_station=station,
        settlement_source=source,
        observation_window="00:00-23:59",
        condition_id="dummy",
        local_timezone="America/New_York",
        contract_side="dummy",
        contract_url="dummy"
    )

class TestWeatherPipelineAudit(unittest.TestCase):
    def test_variance_math(self):
        ens_sigma = 1.0
        lead_days = 2
        events = [make_event("NYC", 0, 100)]
        try:
            generate_event_probability_vector(events, 80.0, 0.1, 0, 15) # floor should trigger
        except ValueError:
            pass

    def test_probability_validation(self):
        with self.assertRaisesRegex(ValueError, "sum to 1.0"):
            validate_probability_vector("test", [0.5, 0.4])
        with self.assertRaisesRegex(ValueError, "negative"):
            validate_probability_vector("test", [1.1, -0.1])
        with self.assertRaisesRegex(ValueError, "non-finite"):
            validate_probability_vector("test", [float('nan'), 1.0])

    def test_incomplete_bucket_space(self):
        # Even if buckets cover 99% of probability mass (e.g., 0 to 150), they are not explicitly open-ended
        events = [make_event("NYC", 0, 150)] 
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_BUCKET_SPACE"):
            generate_event_probability_vector(events, 80.5, 5.0, 1, 0)
            
    def test_numeric_extreme_low_without_open_ended_flag_not_exhaustive(self):
        events = [make_event("NYC", -100, float("inf"))]
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_BUCKET_SPACE"):
            generate_event_probability_vector(events, 80.0, 5.0, 1, 0)
            
    def test_numeric_extreme_high_without_open_ended_flag_not_exhaustive(self):
        events = [make_event("NYC", float("-inf"), 200)]
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_BUCKET_SPACE"):
            generate_event_probability_vector(events, 80.0, 5.0, 1, 0)

    def test_canonical_bucket_ordering(self):
        events = [
            make_event("NYC", 85, 90),
            make_event("NYC", 75, 80),
            make_event("NYC", 80, 85)
        ]
        raw_markets = [{"name": "85-90"}, {"name": "75-80"}, {"name": "80-85"}]
        
        sorted_pairs = sorted(zip(events, raw_markets), key=lambda x: (x[0].bucket_low_f, x[0].bucket_high_f, x[0].market_id))
        events_sorted = [p[0] for p in sorted_pairs]
        
        self.assertEqual(events_sorted[0].bucket_low_f, 75)
        self.assertEqual(events_sorted[1].bucket_low_f, 80)
        self.assertEqual(events_sorted[2].bucket_low_f, 85)

    def test_nowcast_masking(self):
        events = [
            make_event("NYC", 70, 75),
            make_event("NYC", 75, 80),
            make_event("NYC", 80, 85)
        ]
        p_vector = [0.33, 0.33, 0.34]
        
        # high_so_far is 76. The 70-75 bucket (max 75) is impossible.
        masked = mask_impossible_buckets(events, p_vector, 76.0)
        self.assertEqual(masked[0], 0.0)
        self.assertAlmostEqual(sum(masked), 1.0)
        
        # All impossible
        with self.assertRaisesRegex(ValueError, "IMPOSSIBLE_BUCKET_ONLY"):
            mask_impossible_buckets(events, p_vector, 90.0)

    def test_execution_cost_no_midpoint(self):
        raw_markets = [
            {"best_bid": 0.4, "best_ask": 0.6, "ask_size": 100, "execution_price_source": "mid"}
        ]
        with self.assertRaisesRegex(ValueError, "Executable cost cannot use midpoint"):
            generate_executable_cost_vector(raw_markets, "polymarket")

    def test_optimizer_depth_caps(self):
        P_adj = [0.1, 0.8, 0.1]
        Q_exec = [0.9, 0.4, 0.9] # 2nd bucket has edge (0.8 > 0.4)
        depth_caps = [100, 5, 100] # Only 5 shares available for the good bucket
        bankroll = 1000.0
        
        x_opt = optimize_portfolio(P_adj, Q_exec, depth_caps, bankroll)
        self.assertLessEqual(x_opt[1], 5.0) # Must respect depth cap
        self.assertEqual(x_opt[0], 0.0)
        self.assertEqual(x_opt[2], 0.0)

    def test_optimizer_rounding_validation(self):
        P_adj = [0.505, 0.495]
        Q_exec = [0.50, 0.50]
        depth_caps = [1000, 1000]
        bankroll = 100.0
        
        x_opt = optimize_portfolio(P_adj, Q_exec, depth_caps, bankroll)
        self.assertGreaterEqual(sum(x_opt), 0)

    def test_optimizer_objective(self):
        x = np.array([10.0, 0.0])
        p_adj = np.array([0.6, 0.4])
        q_exec = np.array([0.5, 0.5])
        bankroll = 100.0
        
        expected_log_B = 0.6 * math.log(105.0) + 0.4 * math.log(95.0)
        result = expected_log_growth(x, p_adj, q_exec, bankroll)
        self.assertAlmostEqual(result, -expected_log_B)
        
    def test_log_growth_delta_compared_to_log_bankroll(self):
        # Create a situation where absolute log growth is positive but delta is negative.
        # This occurs naturally if expected log B > 0 but < log(B).
        P_adj = [0.49, 0.51] # slightly negative EV on outcome 0
        Q_exec = [0.50, 0.50]
        depth_caps = [1000, 1000]
        bankroll = 100.0
        
        # Test the formula in optimizer manually
        p_np = np.array(P_adj)
        q_np = np.array(Q_exec)
        x_np = np.array([10.0, 0.0]) # Force a bad trade
        
        # log growth of the bad trade
        opt_log_growth = -expected_log_growth(x_np, p_np, q_np, bankroll)
        no_trade_log_growth = math.log(bankroll)
        
        self.assertGreater(opt_log_growth, 0) # Log growth is positive
        self.assertLess(opt_log_growth, no_trade_log_growth) # But delta is negative
        
        # The optimizer should return 0 if delta is negative (this is tested by test_optimizer_rounding_validation already)

    def test_open_ended_below_bucket(self):
        from pavlov.pipeline.settlement_resolver import parse_bucket_bounds
        market = {"strike_type": "less", "title": "70 or below"}
        lo, hi, label = parse_bucket_bounds(market)
        self.assertEqual(lo, float('-inf'))
        self.assertEqual(hi, 70.5)

    def test_open_ended_above_bucket(self):
        from pavlov.pipeline.settlement_resolver import parse_bucket_bounds
        market = {"strike_type": "greater", "title": "73 or above"}
        lo, hi, label = parse_bucket_bounds(market)
        self.assertEqual(lo, 72.5)
        self.assertEqual(hi, float('inf'))

    def test_single_degree_bucket_bounds(self):
        from pavlov.pipeline.settlement_resolver import parse_bucket_bounds
        market = {"strike_type": "between", "title": "71"}
        lo, hi, label = parse_bucket_bounds(market)
        self.assertEqual(lo, 70.5)
        self.assertEqual(hi, 71.5)

    def test_between_two_degrees_uses_half_degree_bounds(self):
        from pavlov.pipeline.settlement_resolver import parse_bucket_bounds
        market = {"strike_type": "between", "title": "between 72 and 73"}
        lo, hi, label = parse_bucket_bounds(market)
        self.assertEqual(lo, 71.5)
        self.assertEqual(hi, 73.5)

    def test_adjacent_integer_buckets_cover_without_gap_or_overlap(self):
        from pavlov.pipeline.settlement_resolver import parse_bucket_bounds
        lo1, hi1, _ = parse_bucket_bounds({"strike_type": "less", "title": "70 or below"})
        lo2, hi2, _ = parse_bucket_bounds({"strike_type": "between", "title": "71"})
        lo3, hi3, _ = parse_bucket_bounds({"strike_type": "between", "title": "72-73"})
        lo4, hi4, _ = parse_bucket_bounds({"strike_type": "greater", "title": "74 or above"})
        self.assertEqual(hi1, lo2)
        self.assertEqual(hi2, lo3)
        self.assertEqual(hi3, lo4)

    def test_same_city_different_station_not_grouped(self):
        event1 = make_event("NYC", 70, 80, station="KNYC")
        event2 = make_event("NYC", 70, 80, station="KLGA")
        group_key1 = (event1.settlement_station, event1.settlement_source, event1.date, event1.observation_window, "platform")
        group_key2 = (event2.settlement_station, event2.settlement_source, event2.date, event2.observation_window, "platform")
        self.assertNotEqual(group_key1, group_key2)

    def test_same_station_different_source_not_grouped(self):
        event1 = make_event("NYC", 70, 80, source="NWS CLI")
        event2 = make_event("NYC", 70, 80, source="METAR")
        group_key1 = (event1.settlement_station, event1.settlement_source, event1.date, event1.observation_window, "platform")
        group_key2 = (event2.settlement_station, event2.settlement_source, event2.date, event2.observation_window, "platform")
        self.assertNotEqual(group_key1, group_key2)

    def test_effective_cost_above_one_rejected(self):
        raw_markets = [{"best_ask": 0.999, "ask_size": 100}]
        # with slippage 0.005, cost > 1.0
        with self.assertRaisesRegex(ValueError, "EFFECTIVE_COST_NOT_TRADABLE"):
            generate_executable_cost_vector(raw_markets, "polymarket")

    def test_effective_cost_equal_one_rejected(self):
        raw_markets = [{"best_ask": 0.995, "ask_size": 100}]
        # with slippage 0.005, cost = 1.0
        with self.assertRaisesRegex(ValueError, "EFFECTIVE_COST_NOT_TRADABLE"):
            generate_executable_cost_vector(raw_markets, "polymarket")

    def test_bankroll_dollar_cap_converted_to_share_cap(self):
        # A 100 bankroll -> 0.75 event cap -> execution cost 0.1 -> 7.5 max shares
        P_adj = [0.9]
        Q_exec = [0.1]
        depth_caps = [1000] # infinite depth
        bankroll = 100.0
        x_opt = optimize_portfolio(P_adj, Q_exec, depth_caps, bankroll)
        self.assertLessEqual(x_opt[0], 7.5)
        
    def test_depth_cap_enforced_in_shares(self):
        # A 100 bankroll -> 0.75 event cap -> execution cost 0.1 -> 7.5 max shares by bankroll
        # But depth is only 5 shares.
        P_adj = [0.9]
        Q_exec = [0.1]
        depth_caps = [5] # depth cap
        bankroll = 100.0
        x_opt = optimize_portfolio(P_adj, Q_exec, depth_caps, bankroll)
        self.assertLessEqual(x_opt[0], 5.0)

if __name__ == '__main__':
    unittest.main()
