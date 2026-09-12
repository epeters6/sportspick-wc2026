"""Midway verification must use complete Chicago days and isolated model names."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase, mock

with mock.patch("backend.db.get_db", return_value=mock.MagicMock()):
    from backend.ml.weather_mos import WeatherMOS
    from backend.ml import weather_verification


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 12, 2, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class TestMidwayTraining(TestCase):
    def test_midway_mos_accepts_completed_day_but_rejects_current_chicago_day(self):
        cutoff = FixedClock.now(timezone.utc)
        row = {"station_id": "KMDW", "target_date": "2026-09-10", "updated_at": "2026-09-11T12:00:00Z"}
        self.assertTrue(WeatherMOS._prior_observation(row, cutoff))
        self.assertFalse(WeatherMOS._prior_observation({**row, "target_date": "2026-09-11"}, cutoff))

    def test_midway_actuals_filter_by_chicago_date_across_utc_midnight(self):
        response = mock.Mock()
        response.json.return_value = [
            {"temp": 35, "reportTime": "2026-09-10T02:00:00Z"},  # Sep 9 Chicago; exclude
            {"temp": 20, "reportTime": "2026-09-10T14:00:00Z"},
            {"temp": 25, "reportTime": "2026-09-11T02:00:00Z"},  # Sep 10 Chicago; include
            {"temp": 36, "reportTime": "2026-09-11T06:00:00Z"},  # Sep 11 Chicago; exclude
        ]
        with mock.patch.object(weather_verification, "datetime", FixedClock), \
             mock.patch("requests.get", return_value=response) as request:
            actual = weather_verification.fetch_actual_extremes("KMDW", "2026-09-10")
        self.assertEqual(actual, {"high": 77.0, "low": 68.0})
        self.assertIn("ids=KMDW", request.call_args.args[0])

    def test_partial_current_station_day_never_becomes_training_actual(self):
        with mock.patch.object(weather_verification, "datetime", FixedClock), \
             mock.patch("requests.get") as request:
            actual = weather_verification.fetch_actual_extremes("KMDW", "2026-09-11")
        self.assertEqual(actual, {"high": None, "low": None})
        request.assert_not_called()

    def test_standard_day_contains_hour_after_civil_midnight(self):
        response = mock.Mock()
        response.json.return_value = [
            {"temp": 35, "reportTime": "2026-09-10T05:30:00Z"},  # Sep 9 CST; exclude
            {"temp": 20, "reportTime": "2026-09-10T14:00:00Z"},
            {"temp": 25, "reportTime": "2026-09-11T05:30:00Z"},  # Sep 10 CST; include
        ]
        with mock.patch.object(weather_verification, "datetime", FixedClock), \
             mock.patch("requests.get", return_value=response):
            actual = weather_verification.fetch_actual_extremes("KMDW", "2026-09-10", model_name="ensemble_station_v2")
        self.assertEqual(actual, {"high": 77.0, "low": 68.0})

    def test_midway_model_waits_until_standard_day_finishes(self):
        cutoff = datetime(2026, 9, 12, 5, 30, tzinfo=timezone.utc)  # 00:30 CDT, 23:30 CST
        row = {"station_id": "KMDW", "target_date": "2026-09-11", "updated_at": "2026-09-12T05:00:00Z",
               "model_name": "ensemble_station_v2"}
        self.assertFalse(WeatherMOS._prior_observation(row, cutoff))
        self.assertTrue(WeatherMOS._prior_observation(row, cutoff.replace(hour=6)))

    def test_station_model_training_query_cannot_use_legacy_model_pool(self):
        rows = [{"station_id": "KMDW", "target_date": "2026-09-10", "updated_at": "2026-09-11T12:00:00Z",
                 "model_name": model, "predicted_high": 70, "actual_high": actual}
                for model, actual in [("ensemble", 90), ("ensemble_station_v2", 72)]]
        calls = []

        class Query:
            def __init__(self):
                self.filters = []
            def select(self, columns): return self
            def eq(self, column, value):
                self.filters.append((column, value))
                calls.append((column, value))
                return self
            def lt(self, *args): return self
            def lte(self, *args): return self
            def order(self, *args, **kwargs): return self
            def limit(self, *args): return self
            def execute(self):
                return SimpleNamespace(data=[r for r in rows if all(r.get(k) == v for k, v in self.filters)])

        engine = WeatherMOS.__new__(WeatherMOS)
        engine.db = SimpleNamespace(table=lambda name: Query())
        engine._cache = {}
        # Omit lead fields only from assertions, not the filtering fixture.
        for row in rows:
            row["lead_time_days"] = 0
        result = engine.calculate_calibration("KMDW", "ensemble_station_v2", 0,
                                              as_of=FixedClock.now(timezone.utc))
        self.assertIn(("model_name", "ensemble_station_v2"), calls)
        self.assertEqual(result.station_samples, 1)
        self.assertEqual(result.pooled_samples, 1)
