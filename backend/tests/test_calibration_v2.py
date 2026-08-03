from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.models.sports.run_shadow_mlb import (
    MLB_MIN_NET_EDGE,
    MLB_MODEL_WEIGHT,
    _record_all_game_predictions,
)
from backend.models.weather.sync_weather import (
    WEATHER_LOW_STAKES_ENABLED,
    WEATHER_MIN_NET_EDGE,
    limit_to_one_weather_position,
    record_weather_prediction_vector,
)
from pavlov.pipeline.market_probability import get_event_lambda
from pavlov.pipeline.probability_model import get_historical_sigma


class TestCalibrationV2(unittest.TestCase):
    def test_weather_trust_decreases_with_lead(self):
        self.assertEqual(get_event_lambda(0), 0.25)
        self.assertEqual(get_event_lambda(1), 0.15)
        self.assertEqual(get_event_lambda(2), 0.10)
        self.assertEqual(get_event_lambda(5), 0.05)

    def test_weather_uncertainty_is_metric_specific_and_wider(self):
        self.assertGreaterEqual(get_historical_sigma(0, 8, "high"), 4.5)
        self.assertGreaterEqual(get_historical_sigma(1, 8, "high"), 5.25)
        self.assertGreater(get_historical_sigma(0, 8, "high"), get_historical_sigma(0, 8, "low"))

    def test_observation_and_edge_guards_default_fail_closed(self):
        self.assertFalse(WEATHER_LOW_STAKES_ENABLED)
        self.assertGreaterEqual(WEATHER_MIN_NET_EDGE, 0.05)
        self.assertLessEqual(MLB_MODEL_WEIGHT, 0.10)
        self.assertGreaterEqual(MLB_MIN_NET_EDGE, 0.05)

    def test_weather_vector_keeps_only_highest_edge_position(self):
        limited = limit_to_one_weather_position(
            [2.0, 3.0, 1.0], [0.30, 0.45, 0.25], [0.20, 0.30, 0.20]
        )
        self.assertEqual(limited, [0.0, 3.0, 0.0])

    def test_weather_persists_every_bucket_not_only_selection(self):
        db = MagicMock()
        events = [
            SimpleNamespace(
                market_id="low", bucket_low_f=float("-inf"),
                bucket_high_f=70.5, bucket_label="70 or below",
            ),
            SimpleNamespace(
                market_id="high", bucket_low_f=70.5,
                bucket_high_f=float("inf"), bucket_label="71 or above",
            ),
        ]
        count = record_weather_prediction_vector(
            db,
            platform="kalshi",
            station="KNYC",
            date_str="2026-08-03",
            metric="high",
            events=events,
            raw_probabilities=[0.4, 0.6],
            decision_probabilities=[0.45, 0.55],
            market_probabilities=[0.5, 0.5],
            executable_costs=[0.52, 0.52],
            selected_shares=[0.0, 1.0],
            theoretical_shares=[0.0, 1.0],
            lead_days=1,
            calibration_metadata={"calibration_source": "test"},
        )
        self.assertEqual(count, 2)
        payload = db.table.return_value.insert.call_args.args[0]
        self.assertEqual(len(payload), 2)
        self.assertEqual({row["outcome"] for row in payload}, {"low", "high"})

    def test_mlb_persists_both_sides_of_every_exact_game(self):
        db = MagicMock()
        candidates = [
            {
                "tradeable": True,
                "selected_team": "New York Yankees",
                "effective_cost": 0.4,
                "market_prob_baseline": 0.38,
                "model_prob": 0.41,
                "venue": "kalshi",
            }
        ]
        count = _record_all_game_predictions(
            db,
            event_id="mlb_ml_2026-08-03_nyy_bos",
            game_pk=123,
            home="New York Yankees",
            away="Boston Red Sox",
            slate_date="2026-08-03",
            probs={"home_prob": 0.62, "away_prob": 0.38, "game_pk": 123},
            candidates=candidates,
            selected=candidates[0],
        )
        self.assertEqual(count, 2)
        payload = db.table.return_value.insert.call_args.args[0]
        self.assertEqual(len(payload), 2)
        self.assertTrue(all(row["event_key"] == "mlb:123" for row in payload))


if __name__ == "__main__":
    unittest.main()
