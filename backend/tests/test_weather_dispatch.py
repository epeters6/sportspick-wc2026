"""Scheduler delay boundaries must never backdate a paper decision."""
from datetime import datetime, timezone
import unittest

from scripts.check_weather_dispatch import check_dispatch


class WeatherDispatchTests(unittest.TestCase):
    now = datetime(2026, 9, 7, 12, 12, tzinfo=timezone.utc)

    def test_current_external_dispatch_and_manual_execution(self):
        for kind in ("weather", "clv"):
            self.assertTrue(check_dispatch(kind, "2026-09-07T12:07:00Z", self.now)[0])
            self.assertTrue(check_dispatch(kind, "", self.now)[0])

    def test_weather_queue_cannot_move_an_old_slot_into_a_new_hour(self):
        self.assertEqual(check_dispatch("weather", "2026-09-07T11:59:59Z", self.now),
                         (False, "expired_weather_hour"))
        self.assertEqual(check_dispatch("weather", "2026-09-06T12:07:00Z", self.now),
                         (False, "expired_weather_hour"))
        self.assertTrue(check_dispatch("weather", "2026-09-07T12:00:00Z", self.now)[0])

    def test_clv_uses_elapsed_time_including_across_hour_boundary(self):
        self.assertFalse(check_dispatch("clv", "2026-09-07T12:06:59Z", self.now)[0])
        now = datetime(2026, 9, 7, 13, 1, tzinfo=timezone.utc)
        self.assertTrue(check_dispatch("clv", "2026-09-07T12:57:00Z", now)[0])

    def test_invalid_naive_non_utc_or_future_input_fails_closed(self):
        for value in ("garbage", "2026-09-07T12:07:00", "2026-09-07T08:07:00-04:00",
                      "2026-09-07T12:13:01Z", "2026-09-08T12:07:00Z", "\nexecute=true"):
            for kind in ("weather", "clv"):
                self.assertFalse(check_dispatch(kind, value, self.now)[0])

    def test_gate_requires_an_aware_clock(self):
        with self.assertRaises(ValueError):
            check_dispatch("weather", "", datetime(2026, 9, 7))

    def test_every_weather_trigger_leaves_the_full_job_budget_in_its_hour(self):
        late = datetime(2026, 9, 7, 12, 35, tzinfo=timezone.utc)
        for value in ("", "2026-09-07T12:27:00Z"):
            self.assertEqual(check_dispatch("weather", value, late),
                             (False, "insufficient_time_in_weather_hour"))
        last_allowed = datetime(2026, 9, 7, 12, 34, 59, tzinfo=timezone.utc)
        self.assertTrue(check_dispatch("weather", "", last_allowed)[0])


if __name__ == "__main__":
    unittest.main()
