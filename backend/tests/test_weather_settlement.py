"""Unit tests for weather paper-bet settlement readiness + grading gates."""
from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.trading.weather_settlement import (
    _actuals_ready_to_grade,
    _city_and_metric_for_bet,
    _target_date_for_bet,
)


def _bet(**kwargs) -> dict:
    base = {
        "id": "test-bet-id-0001",
        "market_id": "KXHIGHTNY-26JUL14-T90",
        "question": "Weather: New York High 90 2026-07-14 (kalshi)",
        "outcome_name": "yes",
        "metadata": {
            "city": "New York",
            "metric": "high",
            "target_date": "2026-07-14",
            "station": "KNYC",
            "bucket_low_f": 89.5,
            "bucket_high_f": 90.5,
        },
    }
    base.update(kwargs)
    return base


class TestWeatherSettlementReadiness(unittest.TestCase):
    def test_target_date_from_metadata(self):
        dt = _target_date_for_bet(_bet())
        self.assertIsNotNone(dt)
        self.assertEqual(dt.date().isoformat(), "2026-07-14")

    def test_target_date_from_kalshi_ticker(self):
        bet = _bet(metadata={}, question="plain", market_id="KXHIGHTNY-26JUL14-T90")
        dt = _target_date_for_bet(bet)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.date().isoformat(), "2026-07-14")

    def test_city_metric_from_question_when_meta_thin(self):
        bet = _bet(
            metadata={"target_date": "2026-07-14"},
            question="Weather: Boston Low <70 2026-07-14 (kalshi)",
        )
        city, metric = _city_and_metric_for_bet(bet)
        self.assertEqual(city, "Boston")
        self.assertEqual(metric, "low")

    def test_high_not_ready_before_local_evening(self):
        # 8pm ET on target day — highs still open
        now = datetime(2026, 7, 14, 20, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(_actuals_ready_to_grade(_bet(), now))

    def test_high_ready_after_local_evening(self):
        now = datetime(2026, 7, 14, 21, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(_actuals_ready_to_grade(_bet(), now))

    def test_high_ready_next_local_day(self):
        now = datetime(2026, 7, 15, 1, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(_actuals_ready_to_grade(_bet(), now))

    def test_low_ready_mid_afternoon_same_day(self):
        bet = _bet(
            metadata={
                "city": "Chicago",
                "metric": "low",
                "target_date": "2026-07-14",
                "station": "KORD",
            },
            question="Weather: Chicago Low 70 2026-07-14 (kalshi)",
            market_id="KXLOWTCHI-26JUL14-B70.5",
        )
        before = datetime(2026, 7, 14, 13, 59, tzinfo=ZoneInfo("America/Chicago"))
        after = datetime(2026, 7, 14, 14, 0, tzinfo=ZoneInfo("America/Chicago"))
        self.assertFalse(_actuals_ready_to_grade(bet, before))
        self.assertTrue(_actuals_ready_to_grade(bet, after))

    def test_not_ready_before_target_date(self):
        now = datetime(2026, 7, 13, 23, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(_actuals_ready_to_grade(_bet(), now))

    def test_missing_target_date_never_ready(self):
        bet = _bet(metadata={}, question="no date here", market_id="not-a-kalshi-ticker")
        now = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(_actuals_ready_to_grade(bet, now))


if __name__ == "__main__":
    unittest.main()
