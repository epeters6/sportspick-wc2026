from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.ml.mlb_calibration import learn_mlb_market_blend
from backend.ml.prediction_evaluation import resolve_mlb_prediction_backlog
from backend.ml.weather_execution_calibration import (
    WeatherExecutionCalibrator,
    _WeatherHistoryRow,
    _probability_bin,
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
        negate_next = False
        for operation, key, value in self.filters:
            if operation == "not_next":
                negate_next = True
                continue
            if operation == "eq":
                matched = row.get(key) == value
            elif operation == "in":
                matched = row.get(key) in value
            elif operation == "is":
                matched = row.get(key) is None if value == "null" else row.get(key) is value
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


class TestWeatherBacklogCalibration(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
