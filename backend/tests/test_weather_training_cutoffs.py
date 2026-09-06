"""Calibration must use only outcomes and observations available by its cutoff."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from backend.ml.weather_execution_calibration import MODEL_VERSION, load_weather_execution_calibrator

# The production module owns a lazy-use singleton; prevent a real client in tests.
with patch("backend.db.get_db", return_value=MagicMock()):
    from backend.ml.weather_mos import WeatherMOS


AS_OF = datetime(2026, 9, 6, 2, tzinfo=timezone.utc)


class Query:
    def __init__(self, db, table):
        self.db, self.name = db, table
    def select(self, columns):
        self.db.calls.append((self.name, "select", columns))
        return self
    def eq(self, key, value):
        self.db.calls.append((self.name, "eq", key, value))
        return self
    def lte(self, key, value):
        self.db.calls.append((self.name, "lte", key, value))
        return self
    def lt(self, key, value):
        self.db.calls.append((self.name, "lt", key, value))
        return self
    def is_(self, *_):
        return self
    @property
    def not_(self):
        return self
    def order(self, *_args, **_kwargs):
        return self
    def limit(self, *_):
        return self
    def execute(self):
        # Deliberately return even future rows to exercise defensive local checks.
        return SimpleNamespace(data=self.db.rows.get(self.name, []))


class Database:
    def __init__(self, **rows):
        self.rows, self.calls = rows, []
    def table(self, name):
        return Query(self, name)


def prediction(resolved, venue="kalshi"):
    return {"source": MODEL_VERSION, "prob": 0.6, "is_correct": True, "resolved_at": resolved,
            "metadata": {"label_source": "venue_official", "eligible_before_observation_gate": True,
                         "metric": "high", "platform": venue, "bucket_low_f": 80, "bucket_high_f": 81,
                         "executable_cost": 0.4}}


def close(observed, venue="kalshi"):
    return {"platform": venue, "entry_market_price": 0.5, "obs_close_price": 0.55,
            "entry_ts": "2026-09-04T10:00:00Z", "obs_close_ts": observed, "status_close": "observed",
            "metadata": {"model_version": MODEL_VERSION}}


def observation(target="2026-09-04", updated="2026-09-05T10:00:00Z", station="KNYC"):
    return {"station_id": station, "target_date": target, "updated_at": updated,
            "predicted_high": 70, "actual_high": 72, "predicted_low": 50, "actual_low": 51}


class TestWeatherExecutionTrainingCutoff(unittest.TestCase):
    def test_only_valid_prior_official_outcomes_and_closes_are_used(self):
        predictions = [prediction("2026-09-06T01:00:00Z"), prediction(AS_OF.isoformat()),
                       prediction("2026-09-06T03:00:00Z"), prediction(None), prediction("bad"),
                       prediction("2026-09-06T01:00:00"), prediction("2026-09-06T01:00:00Z", "polymarket")]
        closes = [close("2026-09-06T01:00:00Z"), close("2026-09-06T03:00:00Z"), close(None),
                  close("bad"), close("2026-09-06T01:00:00Z", "polymarket")]
        db = Database(model_predictions=predictions, clv_obligations=closes)
        calibrator = load_weather_execution_calibrator(db, venue="kalshi", as_of=AS_OF)
        self.assertEqual(len(calibrator.rows), 2)
        self.assertEqual(calibrator.close_clv_samples, 1)
        self.assertAlmostEqual(calibrator.average_close_clv, 0.05)
        self.assertEqual(calibrator.training_as_of, AS_OF.isoformat())
        self.assertIn(("model_predictions", "lte", "resolved_at", AS_OF.isoformat()), db.calls)
        self.assertIn(("clv_obligations", "lte", "obs_close_ts", AS_OF.isoformat()), db.calls)
        result = calibrator.calibrate(metric="high", bucket_low_f=80, bucket_high_f=81,
                                      probability=0.6, executable_cost=0.4, min_net_edge=0.05)
        self.assertEqual(result.as_metadata()["execution_training_as_of"], AS_OF.isoformat())

    def test_future_profitable_rows_cannot_make_a_cohort_ready(self):
        rows = [prediction("2026-09-07T00:00:00Z") for _ in range(100)]
        calibrator = load_weather_execution_calibrator(Database(model_predictions=rows), venue="kalshi", as_of=AS_OF)
        result = calibrator.calibrate(metric="high", bucket_low_f=80, bucket_high_f=81,
                                      probability=0.6, executable_cost=0.4, min_net_edge=0.05)
        self.assertFalse(result.ready)
        self.assertFalse(result.allowed)
        self.assertEqual(result.samples, 0)

    def test_default_cutoff_is_current_utc_and_naive_cutoff_is_rejected(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        calibrator = load_weather_execution_calibrator(Database(model_predictions=[prediction(future.isoformat())]))
        self.assertEqual(len(calibrator.rows), 0)
        self.assertIsNotNone(datetime.fromisoformat(calibrator.training_as_of).tzinfo)
        with self.assertRaisesRegex(ValueError, "REQUIRES_TIMEZONE"):
            load_weather_execution_calibrator(Database(), as_of=datetime(2026, 9, 6))


class TestWeatherMOSTrainingCutoff(unittest.TestCase):
    def engine(self, rows):
        engine = WeatherMOS.__new__(WeatherMOS)
        engine.db, engine._cache = Database(weather_verification=rows), {}
        return engine

    def test_current_local_day_and_actuals_updated_after_cutoff_are_excluded(self):
        rows = [observation(), observation("2026-09-05"), observation("2026-09-06"),
                observation("2026-09-03", "2026-09-06T03:00:00Z"), observation("2026-09-03", None),
                observation("2026-09-03", "2026-09-05T10:00:00"), observation(station="UNKNOWN")]
        engine = self.engine(rows)
        result = engine.calculate_calibration("KNYC", "ensemble", 1, as_of=AS_OF)
        self.assertEqual(result.pooled_samples, 1)
        self.assertEqual(result.station_samples, 1)
        self.assertEqual(result.training_as_of, AS_OF.isoformat())
        self.assertIn(("weather_verification", "lt", "target_date", "2026-09-06"), engine.db.calls)
        self.assertIn(("weather_verification", "lte", "updated_at", AS_OF.isoformat()), engine.db.calls)

    def test_cache_is_scoped_to_cutoff_and_cannot_reuse_future_training(self):
        engine = self.engine([observation(), observation("2026-09-05", "2026-09-05T23:00:00Z")])
        later = AS_OF + timedelta(hours=12)
        later_result = engine.calculate_calibration("KNYC", "ensemble", 1, as_of=later)
        earlier_result = engine.calculate_calibration("KNYC", "ensemble", 1, as_of=AS_OF)
        self.assertEqual(later_result.pooled_samples, 2)
        self.assertEqual(earlier_result.pooled_samples, 1)
        self.assertIs(engine.calculate_calibration("KNYC", "ensemble", 1, as_of=AS_OF), earlier_result)

    def test_future_actuals_do_not_shift_bias(self):
        rows = [observation("2026-09-01", "2026-09-02T12:00:00Z") for _ in range(8)]
        poisoned = observation("2026-09-07", "2026-09-07T12:00:00Z")
        poisoned["actual_high"] = 200
        engine = self.engine(rows + [deepcopy(poisoned) for _ in range(100)])
        result = engine.calculate_calibration("KNYC", "ensemble", 1, as_of=AS_OF)
        self.assertEqual(result.pooled_samples, 8)
        self.assertAlmostEqual(result.bias_correction, 2)
        with self.assertRaisesRegex(ValueError, "REQUIRES_TIMEZONE"):
            engine.calculate_calibration("KNYC", "ensemble", 1, as_of=datetime(2026, 9, 6))


if __name__ == "__main__":
    unittest.main()
