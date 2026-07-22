"""Orchestrator → shadow integration using exact setup_daily_slate manifest schema."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

from backend.models.sports.run_shadow_mlb import (
    PREGAME_MODEL_UNAVAILABLE,
    MLB_SHADOW_ZERO_PROCESSED,
    MLB_MONEYLINE_MANIFEST_EMPTY,
    _pitcher_outs_prob,
    report_pitcher_outs_availability,
    run_mlb_shadow_execution,
)


def _orchestrator_manifest_entry(*, slate_date: str, prediction=None) -> dict:
    """Exact field set written by setup_daily_slate (see orchestrator.py)."""
    return {
        "contract_type": "pitcher_outs",
        "prop_side": "UNDER",
        "name": "Spencer Strider",
        "pitcher_id": 12345,
        "team": "ATL",
        "opponent": "NYM",
        "pitch_hand": "R",
        "baseline": {"expected_outs_baseline": 16.2},
        "prop_line": 17.5,
        "line_movement": {
            "opening_line": 17.5,
            "previous_line": 17.5,
            "current_line": 17.5,
            "line_move_delta": 0.0,
            "line_move_abs": 0.0,
            "last_move_delta": 0.0,
            "book_count": 0,
            "line_last_updated_utc": datetime.now(timezone.utc).isoformat(),
        },
        "tier": 2,
        "tier_reason": "test",
        "bullpen_status": "Fresh",
        "bullpen_context": {},
        "advanced_context": {"manager_hook_score": 0.4},
        "opponent_context": {},
        "environment_context": {"weather_impact": "Normal"},
        "matchup_context": {
            "home_away": "home",
            "venue_team": "ATL",
            "game_pk": 999,
            "scheduled_start_utc": (
                datetime.now(timezone.utc) + timedelta(hours=6)
            ).isoformat(),
        },
        "weather_impact": "Normal",
        "prediction": prediction,
        "slate_date": slate_date,
        "starter_profile": {"expected_outs_baseline": 16.2},
        "model_version": "mlb_pitcher_outs_v4",
        "feature_version": "mlb_quant_manifest_v1",
        "coefficient_source": "under_model_state.json",
        "calibration_status": "uncalibrated_shadow",
        "manifest_updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


class TestOrchestratorShadowIntegration(unittest.TestCase):
    def test_pregame_unavailable_on_orchestrator_schema_without_prediction(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = _orchestrator_manifest_entry(slate_date=today, prediction=None)
        p, meta = _pitcher_outs_prob(entry, "UNDER")
        self.assertEqual(p, 0.0)
        self.assertEqual(meta["rejection"], PREGAME_MODEL_UNAVAILABLE)

    def test_pitcher_outs_unavailable_does_not_block_moneyline_entrypoint(self):
        """Moneyline path runs; pitcher-outs only appears as a report field."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        manifest = {
            "12345": _orchestrator_manifest_entry(slate_date=today, prediction=None),
        }
        moneyline_report = {
            "strategy": "mlb_moneyline",
            "slate_size": 1,
            "processed": 0,
            "rejected": 1,
            "candidate_evaluations": 2,
            "exposed_events": 1,
            "by_venue": {"polymarket": {}, "kalshi": {}},
        }
        with patch(
            "backend.models.sports.run_shadow_mlb.report_pitcher_outs_availability",
            return_value={
                "strategy": "mlb_pitcher_outs",
                "enabled": False,
                "availability": PREGAME_MODEL_UNAVAILABLE,
                "rejection": PREGAME_MODEL_UNAVAILABLE,
            },
        ), patch(
            "backend.models.sports.run_shadow_mlb.run_mlb_moneyline_shadow",
            new=AsyncMock(return_value=moneyline_report),
        ):
            report = asyncio.run(run_mlb_shadow_execution())
        self.assertEqual(report["pitcher_outs"]["rejection"], PREGAME_MODEL_UNAVAILABLE)
        self.assertEqual(report["moneyline"]["strategy"], "mlb_moneyline")

    def test_moneyline_zero_processed_not_mislabeled_as_pregame(self):
        moneyline_report = {
            "strategy": "mlb_moneyline",
            "slate_size": 1,
            "processed": 0,
            "rejected": 1,
            "candidate_evaluations": 2,
            "exposed_events": 0,
            "by_venue": {"polymarket": {"rejected": 2}, "kalshi": {"rejected": 2}},
        }
        with patch(
            "backend.models.sports.run_shadow_mlb.report_pitcher_outs_availability",
            return_value={"rejection": PREGAME_MODEL_UNAVAILABLE},
        ), patch(
            "backend.models.sports.run_shadow_mlb.run_mlb_moneyline_shadow",
            new=AsyncMock(return_value=moneyline_report),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(run_mlb_shadow_execution())
        msg = str(ctx.exception)
        self.assertIn(MLB_SHADOW_ZERO_PROCESSED, msg)
        self.assertNotIn(PREGAME_MODEL_UNAVAILABLE, msg)

    def test_empty_moneyline_slate_raises_manifest_empty(self):
        with patch(
            "backend.models.sports.run_shadow_mlb.report_pitcher_outs_availability",
            return_value={},
        ), patch(
            "backend.models.sports.run_shadow_mlb.run_mlb_moneyline_shadow",
            new=AsyncMock(
                side_effect=RuntimeError(f"{MLB_MONEYLINE_MANIFEST_EMPTY}: none")
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(run_mlb_shadow_execution())
        self.assertIn(MLB_MONEYLINE_MANIFEST_EMPTY, str(ctx.exception))

    def test_in_game_prediction_on_orchestrator_schema_accepted(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = _orchestrator_manifest_entry(
            slate_date=today,
            prediction={"under_proba": 0.58, "over_proba": 0.42},
        )
        p, meta = _pitcher_outs_prob(entry, "UNDER")
        self.assertAlmostEqual(p, 0.58)
        self.assertEqual(meta["prob_method"], "in_game_fatigue_prediction")

    def test_pitcher_outs_report_field(self):
        r = report_pitcher_outs_availability(manifest={})
        self.assertEqual(r["strategy"], "mlb_pitcher_outs")
        self.assertEqual(r["rejection"], PREGAME_MODEL_UNAVAILABLE)
