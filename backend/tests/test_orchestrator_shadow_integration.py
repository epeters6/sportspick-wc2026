"""Orchestrator → shadow integration using exact setup_daily_slate manifest schema."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

from backend.models.sports.run_shadow_mlb import (
    PREGAME_MODEL_UNAVAILABLE,
    _pitcher_outs_prob,
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

    def test_run_shadow_raises_pregame_unavailable_not_zero_success(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        manifest = {
            "12345": _orchestrator_manifest_entry(slate_date=today, prediction=None),
        }
        with patch(
            "backend.ml.mlb_quant.orchestrator.load_existing_manifest",
            return_value=manifest,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(run_mlb_shadow_execution())
        self.assertIn(PREGAME_MODEL_UNAVAILABLE, str(ctx.exception))

    def test_in_game_prediction_on_orchestrator_schema_accepted(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = _orchestrator_manifest_entry(
            slate_date=today,
            prediction={"under_proba": 0.58, "over_proba": 0.42},
        )
        p, meta = _pitcher_outs_prob(entry, "UNDER")
        self.assertAlmostEqual(p, 0.58)
        self.assertEqual(meta["prob_method"], "in_game_fatigue_prediction")
