from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from scripts.daily_shadow_report import build_embed, build_report, previous_local_day


class TestDailyShadowReport(unittest.TestCase):
    def test_previous_et_calendar_day_across_utc_boundary(self):
        report_date, start, end = previous_local_day(
            datetime(2026, 7, 23, 5, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(report_date.isoformat(), "2026-07-22")
        self.assertEqual(start.isoformat(), "2026-07-22T04:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-07-23T04:00:00+00:00")

    def test_event_date_filter_and_strategy_separation(self):
        rows = [
            {
                "event_date": "2026-07-22",
                "strategy": "legacy_consensus_mlb",
                "status": "won",
                "stake": 1.0,
                "pnl": 1.0,
            },
            {
                "event_date": "2026-07-22",
                "strategy": "weather_high",
                "status": "lost",
                "stake": 2.0,
                "pnl": -2.0,
            },
            {
                "event_date": "2026-07-22",
                "strategy": "weather_low",
                "status": "won",
                "stake": 2.0,
                "pnl": 1.5,
            },
            {
                "event_date": "2026-07-21",
                "strategy": "legacy_consensus_mlb",
                "status": "won",
                "stake": 100.0,
                "pnl": 100.0,
            },
        ]
        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
        with patch(
            "scripts.daily_shadow_report.settlement_integrity_datasets",
            return_value={
                "verified_rows": rows,
                "invalid_rows": [],
                "unverifiable_rows": [],
            },
        ), patch(
            "scripts.daily_shadow_report._fetch_phase4_rows",
            return_value=[],
        ), patch(
            "scripts.daily_shadow_report.assess_live_readiness",
            return_value={"message": "legacy blocked"},
        ), patch(
            "backend.trading.live_toggle.is_live_mode",
            return_value=False,
        ):
            report = build_report(
                now_utc=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
                db=db,
                refresh_guardian=False,
            )
        self.assertEqual(len(report["by_strategy"]["legacy_consensus_mlb"]), 1)
        self.assertEqual(len(report["by_strategy"]["weather_high"]), 1)
        self.assertEqual(len(report["by_strategy"]["weather_low"]), 1)
        embed = build_embed(report)
        self.assertIn("Legacy MLB", embed["description"])
        self.assertIn("Phase 4 MLB moneyline", embed["description"])
        self.assertIn("Weather high", embed["description"])
        self.assertIn("Weather low", embed["description"])
        self.assertIn("Live trading status: **OFF**", embed["description"])
        self.assertNotIn("READY FOR LIVE", embed["description"])


if __name__ == "__main__":
    unittest.main()
