from __future__ import annotations

import unittest
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.ml.mlb_calibration import learn_mlb_market_blend
from backend.ml.prediction_evaluation import (
    resolve_mlb_prediction_backlog,
    resolve_weather_prediction_backlog,
    resolve_weather_prediction_rows,
)
from backend.ml.weather_execution_calibration import (
    WeatherExecutionCalibrator,
    _WeatherHistoryRow,
    _probability_bin,
    load_weather_execution_calibrator,
)


class _Query:
    def __init__(self, db, table_name: str):
        self.db = db
        self.table_name = table_name
        self.filters: list[tuple[str, str, object]] = []
        self.payload = None
        self.limit_count = None

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def is_(self, key, value):
        self.filters.append(("is", key, value))
        return self

    def in_(self, key, values):
        self.filters.append(("in", key, list(values)))
        return self

    def like(self, key, value):
        self.filters.append(("like", key, value))
        return self

    @property
    def not_(self):
        self.filters.append(("not_next", "", None))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.limit_count = int(value)
        return self

    def update(self, payload):
        self.payload = dict(payload)
        return self

    def _matches(self, row):
        def value_for(key):
            if "->>" not in key:
                return row.get(key)
            root, nested = key.split("->>", 1)
            value = (row.get(root) or {}).get(nested)
            if isinstance(value, bool):
                return str(value).lower()
            return None if value is None else str(value)

        negate_next = False
        for operation, key, value in self.filters:
            if operation == "not_next":
                negate_next = True
                continue
            if operation == "eq":
                matched = value_for(key) == value
            elif operation == "in":
                matched = value_for(key) in value
            elif operation == "is":
                candidate = value_for(key)
                matched = candidate is None if value == "null" else candidate is value
            elif operation == "like":
                pattern = re.escape(str(value)).replace("%", ".*")
                matched = re.fullmatch(pattern, str(value_for(key) or "")) is not None
            else:
                matched = True
            if negate_next:
                matched = not matched
                negate_next = False
            if not matched:
                return False
        return True

    def execute(self):
        rows = self.db.tables[self.table_name]
        matches = [row for row in rows if self._matches(row)]
        if self.payload is not None:
            for row in matches:
                row.update(self.payload)
            return SimpleNamespace(data=[dict(row) for row in matches])
        if self.limit_count is not None:
            matches = matches[: self.limit_count]
        return SimpleNamespace(data=[dict(row) for row in matches])


class _DB:
    def __init__(self, **tables):
        self.tables = tables

    def table(self, name):
        return _Query(self, name)


class TestMlbBacklogTraining(unittest.TestCase):
    def test_grades_all_game_predictions_without_a_selected_bet(self):
        predictions = [
            {"id": "p1", "source": "mlb_quant_all_games_v2", "event_key": "mlb:123", "outcome": "New York Yankees", "resolved_at": None},
            {"id": "p2", "source": "mlb_quant_all_games_v2", "event_key": "mlb:123", "outcome": "Boston Red Sox", "resolved_at": None},
            {"id": "p3", "source": "mlb_quant_all_games_v2", "event_key": "mlb:999", "outcome": "Chicago Cubs", "resolved_at": None},
            {"id": "p4", "source": "mlb_quant_all_games_v2", "event_key": "mlb:999", "outcome": "St. Louis Cardinals", "resolved_at": None},
        ]
        matches = [
            {"sport": "mlb", "external_id": "mlb_123", "winner": "New York Yankees", "is_final": True},
            {"sport": "mlb", "external_id": "mlb_999", "winner": None, "is_final": False},
        ]
        db = _DB(model_predictions=predictions, matches=matches)
        summary = resolve_mlb_prediction_backlog(
            db,
            resolved_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["matched_events"], 1)
        self.assertEqual(summary["resolved_rows"], 2)
        self.assertTrue(predictions[0]["is_correct"])
        self.assertFalse(predictions[1]["is_correct"])
        self.assertIsNone(predictions[2]["resolved_at"])

    def test_learned_mlb_weight_drops_to_market_when_raw_model_is_worse(self):
        rows = []
        for event in range(120):
            for side, raw, market, correct in (
                ("home", 0.40, 0.60, True),
                ("away", 0.60, 0.40, False),
            ):
                rows.append(
                    {
                        "source": "mlb_quant_all_games_v2",
                        "event_key": f"mlb:{event}",
                        "outcome": side,
                        "prob": raw,
                        "market_price": market,
                        "is_correct": correct,
                        "resolved_at": "2026-08-01T00:00:00+00:00",
                    }
                )
        calibration = learn_mlb_market_blend(_DB(model_predictions=rows))
        self.assertEqual(calibration.status, "learned_all_game_brier_v1")
        self.assertEqual(calibration.sample_events, 120)
        self.assertEqual(calibration.model_weight, 0.0)
        self.assertLess(calibration.market_brier, calibration.raw_brier)


class TestWeatherBacklogCalibration(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _row(metric, direction, probability, cost, correct):
        return _WeatherHistoryRow(
            metric=metric,
            direction=direction,
            probability_bin=_probability_bin(probability),
            probability=probability,
            executable_cost=cost,
            correct=correct,
        )

    def test_blocks_unprofitable_high_tail_and_applies_close_clv_penalty(self):
        rows = [self._row("high", "above", 0.12, 0.04, False) for _ in range(30)]
        calibrator = WeatherExecutionCalibrator(
            rows,
            close_clv_samples=50,
            average_close_clv=-0.02,
        )
        result = calibrator.calibrate(
            metric="high",
            bucket_low_f=100.5,
            bucket_high_f=None,
            probability=0.12,
            executable_cost=0.04,
            min_net_edge=0.05,
        )
        self.assertTrue(result.ready)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "WEATHER_EXECUTION_COHORT_NOT_PROFITABLE")
        self.assertAlmostEqual(result.close_clv_penalty, 0.02)
        self.assertLess(result.execution_probability, 0.12)

    def test_allows_only_backlog_profitable_low_cohort(self):
        good_rows = [
            self._row("low", "middle", 0.18, 0.05, index < 10)
            for index in range(20)
        ]
        calibrator = WeatherExecutionCalibrator(
            good_rows,
            close_clv_samples=50,
            average_close_clv=-0.02,
        )
        result = calibrator.calibrate(
            metric="low",
            bucket_low_f=60.5,
            bucket_high_f=61.5,
            probability=0.18,
            executable_cost=0.05,
            min_net_edge=0.05,
        )
        self.assertTrue(result.allowed)
        self.assertGreater(result.historical_unit_roi, 0.0)
        self.assertAlmostEqual(result.execution_probability, 0.16)

    def test_station_verification_is_preserved_but_not_a_trading_label(self):
        predictions = [
            {
                "id": "w1",
                "source": "weather_calibrated_v2",
                "event_key": "weather:kalshi:KNYC:2026-08-27:high",
                "is_correct": True,
                "resolved_at": "2026-08-28T01:00:00+00:00",
                "metadata": {"bucket_low_f": 89.5, "bucket_high_f": 90.5},
            }
        ]
        db = _DB(model_predictions=predictions)
        updated = resolve_weather_prediction_rows(
            db,
            station="KNYC",
            target_date="2026-08-27",
            metric="high",
            actual=90.0,
            resolved_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
        self.assertEqual(updated, 1)
        self.assertIsNone(predictions[0]["is_correct"])
        self.assertIsNone(predictions[0]["resolved_at"])
        self.assertTrue(predictions[0]["metadata"]["meteorological_is_correct"])
        self.assertEqual(
            predictions[0]["metadata"]["meteorological_source"], "station_metar"
        )

    async def test_official_backlog_relabels_legacy_station_result(self):
        predictions = [
            {
                "id": "w2",
                "source": "weather_calibrated_v2",
                "event_key": "weather:kalshi:KIAH:2026-08-29:low",
                "outcome": "KXLOWTHOU-26AUG29-B74.5",
                "is_correct": True,
                "resolved_at": "2026-08-29T18:00:00+00:00",
                "metadata": {
                    "platform": "kalshi",
                    "eligible_before_observation_gate": True,
                    "metric": "low",
                },
            }
        ]

        async def official(venue, market_id):
            return {
                "resolved": True,
                "winner": "no",
                "market_id": market_id,
                "settled_at": "2026-08-30T02:00:00+00:00",
                "rules_primary": "The Weather Company CLIHOU",
            }

        summary = await resolve_weather_prediction_backlog(
            _DB(model_predictions=predictions),
            resolution_fetcher=official,
        )
        self.assertEqual(summary["resolved_rows"], 1)
        self.assertFalse(predictions[0]["is_correct"])
        self.assertEqual(predictions[0]["metadata"]["label_source"], "venue_official")
        self.assertTrue(predictions[0]["metadata"]["meteorological_is_correct"])

    def test_execution_calibrator_uses_only_official_labels(self):
        base_meta = {
            "eligible_before_observation_gate": True,
            "metric": "high",
            "bucket_low_f": 89.5,
            "bucket_high_f": 90.5,
            "executable_cost": 0.4,
        }
        predictions = [
            {
                "source": "weather_calibrated_v2",
                "prob": 0.6,
                "is_correct": True,
                "resolved_at": "2026-08-30T01:00:00+00:00",
                "metadata": {**base_meta, "label_source": "station_metar"},
            },
            {
                "source": "weather_calibrated_v2",
                "prob": 0.6,
                "is_correct": False,
                "resolved_at": "2026-08-30T02:00:00+00:00",
                "metadata": {**base_meta, "label_source": "venue_official"},
            },
        ]
        calibrator = load_weather_execution_calibrator(
            _DB(model_predictions=predictions, clv_obligations=[])
        )
        self.assertEqual(len(calibrator.rows), 1)
        self.assertFalse(calibrator.rows[0].correct)


if __name__ == "__main__":
    unittest.main()
