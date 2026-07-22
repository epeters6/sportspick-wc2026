"""Focused Phase 2 repair tests: duplicate ids, CLV stub, analysis honesty.

These tests must not require real Supabase secrets at import time.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


class TestWeatherCandidateId(unittest.TestCase):
    def test_deterministic_format(self):
        from backend.models.weather.sync_weather import weather_candidate_id

        cid = weather_candidate_id(
            "kalshi", "KNYC", "2026-07-21", "high", "KXHIGHNY-26JUL21-T84", "paper"
        )
        self.assertEqual(
            cid,
            "kalshi:KNYC:2026-07-21:high:KXHIGHNY-26JUL21-T84:yes:paper",
        )

    def test_mode_and_metric_change_id(self):
        from backend.models.weather.sync_weather import weather_candidate_id

        a = weather_candidate_id("kalshi", "KNYC", "2026-07-21", "high", "M1", "paper")
        b = weather_candidate_id("kalshi", "KNYC", "2026-07-21", "low", "M1", "paper")
        c = weather_candidate_id("kalshi", "KNYC", "2026-07-21", "high", "M1", "live")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)


class TestAnalyzeSportsShadowMissingManifest(unittest.TestCase):
    def test_missing_decisions_raises(self):
        from scripts.analyze_sports_shadow import run_analysis

        missing = os.path.join(tempfile.gettempdir(), "no_such_sports_shadow_decisions.jsonl")
        if os.path.exists(missing):
            os.remove(missing)
        with self.assertRaises(FileNotFoundError):
            run_analysis(decisions_file=missing)


class TestClvObligationUpsert(unittest.TestCase):
    def test_init_calls_upsert_fail_soft(self):
        from pavlov.pipeline.clv_tracker import init_clv_record

        with patch("pavlov.pipeline.clv_tracker._upsert_clv_obligation") as upsert:
            rec = init_clv_record(
                "cid1", "m1", "yes", "YES", 0.42,
                datetime.now(timezone.utc),
                platform="kalshi",
            )
            self.assertEqual(rec.trade_id, "cid1")
            upsert.assert_called_once()
            self.assertEqual(upsert.call_args.kwargs.get("platform"), "kalshi")

    def test_upsert_swallows_db_errors(self):
        from pavlov.pipeline.clv_tracker import CLVRecord, _upsert_clv_obligation

        rec = CLVRecord(
            trade_id="t",
            market_id="m",
            outcome_id="yes",
            side="YES",
            entry_price=0.5,
            entry_time=datetime.now(timezone.utc),
        )
        with patch("backend.db.get_db", side_effect=RuntimeError("no creds")):
            # Must not raise
            _upsert_clv_obligation(rec, platform="kalshi")


class TestSettlementResolutionSource(unittest.TestCase):
    def test_apply_resolution_stores_source(self):
        # Import settlement helper only after confirming module import is secret-free.
        # poly_client is lazy; get_db is not called at import.
        import backend.trading.weather_settlement as ws

        self.assertTrue(callable(ws._apply_resolution))
        db = MagicMock()
        db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()
        bet = {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "market_id": "KXHIGH",
            "metadata": {"station": "KNYC"},
        }
        now = datetime.now(timezone.utc)
        ok = ws._apply_resolution(
            db, bet, "won", 1.5, now, "graded vs observed temp",
            resolution_source="station_actual",
        )
        self.assertTrue(ok)
        update_payload = db.table.return_value.update.call_args[0][0]
        self.assertEqual(update_payload["metadata"]["resolution_source"], "station_actual")


class TestPitcherOutsNoLogisticFallback(unittest.TestCase):
    def test_missing_prediction_rejects(self):
        from backend.models.sports.run_shadow_mlb import _pitcher_outs_prob

        p, meta = _pitcher_outs_prob({"prop_line": 17.5}, "UNDER")
        self.assertEqual(p, 0.0)
        self.assertEqual(meta.get("rejection"), "PITCHER_OUTS_PRED_MISSING")

    def test_engine_prediction_used(self):
        from backend.models.sports.run_shadow_mlb import _pitcher_outs_prob

        p, meta = _pitcher_outs_prob(
            {"prediction": {"under_proba": 0.62, "over_proba": 0.38}},
            "UNDER",
        )
        self.assertAlmostEqual(p, 0.62)
        self.assertIsNone(meta.get("rejection"))
        self.assertEqual(meta.get("prob_method"), "pitcher_outs_engine")


class TestGuardianHaltFailClosed(unittest.TestCase):
    def test_unreadable_supabase_fails_closed(self):
        from backend.trading.live_toggle import _guardian_halted

        with patch("backend.trading.live_toggle.get_db", side_effect=RuntimeError("boom")):
            halted, state = _guardian_halted()
        self.assertTrue(halted)
        self.assertEqual(state.get("source"), "fail_closed")


class TestLiveRemainsDisabled(unittest.TestCase):
    def test_env_locks_off(self):
        self.assertNotEqual(os.environ.get("POLYMARKET_LIVE_ENABLED", "false").lower(), "true")
        self.assertNotEqual(os.environ.get("LIVE_TRADING_ENABLED", "false").lower(), "true")


if __name__ == "__main__":
    unittest.main()
