"""Unit tests for weather paper-bet settlement readiness + grading gates."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from backend.trading.weather_settlement import (
    _actuals_ready_to_grade,
    _apply_resolution,
    _city_and_metric_for_bet,
    _grade_bet_against_actual,
    _grade_bet_against_venue,
    _target_date_for_bet,
    check_kalshi_resolution,
)
from backend.trading.settlement_integrity import WEATHER_SETTLEMENT_VERSION


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

    def test_high_not_ready_same_day_even_after_local_evening(self):
        now = datetime(2026, 7, 14, 21, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(_actuals_ready_to_grade(_bet(), now))

    def test_high_ready_next_local_day(self):
        now = datetime(2026, 7, 15, 1, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(_actuals_ready_to_grade(_bet(), now))

    def test_low_not_ready_same_day(self):
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
        self.assertFalse(_actuals_ready_to_grade(bet, after))

    def test_not_ready_before_target_date(self):
        now = datetime(2026, 7, 13, 23, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(_actuals_ready_to_grade(_bet(), now))

    def test_missing_target_date_never_ready(self):
        bet = _bet(metadata={}, question="no date here", market_id="not-a-kalshi-ticker")
        now = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(_actuals_ready_to_grade(bet, now))

    def test_actual_grade_remains_a_temperature_diagnostic(self):
        bet = _bet(stake=5.0, shares=10.0, market_price=0.5)
        with patch(
            "backend.ml.weather_verification.fetch_actual_extremes",
            return_value={"high": 90.0, "low": 70.0},
        ):
            evidence = _grade_bet_against_actual(
                bet,
                datetime(2026, 7, 15, tzinfo=timezone.utc),
            )
        self.assertEqual(evidence["status"], "won")
        self.assertEqual(evidence["actual_temp_f"], 90.0)
        self.assertEqual(evidence["station"], "KNYC")
        self.assertTrue(evidence["in_bucket"])

    def test_official_result_controls_settlement_and_fee_correct_pnl(self):
        bet = _bet(stake=5.2, shares=10.0, market_price=0.5)
        evidence = _grade_bet_against_venue(
            bet,
            {
                "resolved": True,
                "winner": "yes",
                "market_id": bet["market_id"],
                "settled_at": "2026-07-15T02:00:00Z",
                "rules_primary": "Official venue source",
            },
            "kalshi",
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["status"], "won")
        self.assertEqual(evidence["pnl"], 4.8)

        db = MagicMock()
        db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()
        now = datetime(2026, 7, 15, tzinfo=timezone.utc)
        self.assertTrue(
            _apply_resolution(
                db,
                bet,
                evidence["status"],
                evidence["pnl"],
                now,
                "exchange:kalshi",
                resolution_source="kalshi_official",
                settlement_evidence=evidence,
            )
        )
        payload = db.table.return_value.update.call_args[0][0]
        settlement = payload["metadata"]["settlement"]
        self.assertEqual(settlement["version"], WEATHER_SETTLEMENT_VERSION)
        self.assertEqual(settlement["source"], "kalshi_official")
        self.assertEqual(settlement["official_result"], "yes")
        self.assertEqual(payload["settlement_version"], WEATHER_SETTLEMENT_VERSION)


class TestWeatherVenueResolution(unittest.IsolatedAsyncioTestCase):
    async def test_kalshi_finalized_is_a_resolved_official_result(self):
        client = MagicMock()
        client._get = AsyncMock(
            return_value={
                "market": {
                    "status": "finalized",
                    "result": "no",
                    "settlement_ts": "2026-08-30T02:00:00Z",
                    "settlement_value_dollars": "0.0000",
                    "rules_primary": "The Weather Company CLIHOU",
                }
            }
        )
        with patch(
            "backend.trading.weather_settlement.KalshiClient", return_value=client
        ):
            result = await check_kalshi_resolution("KXLOWTHOU-26AUG29-B74.5")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["winner"], "no")
        self.assertEqual(result["market_id"], "KXLOWTHOU-26AUG29-B74.5")


if __name__ == "__main__":
    unittest.main()
