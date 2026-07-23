from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from scripts.analyze_sports_shadow import summarize_settled_outcomes
from scripts.settle_sports_shadow import (
    SHADOW_SETTLEMENT_GAME_PK_MISMATCH,
    settle_pending,
)


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self.payload = None
        self.filters = {}

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def in_(self, key, value):
        self.filters[key] = value
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def execute(self):
        result = MagicMock()
        if self.payload is not None:
            self.db.updates.append(self.payload)
            result.data = []
        elif self.table_name == "clv_obligations":
            result.data = self.db.obligations
        else:
            result.data = self.db.matches
        return result


class _DB:
    def __init__(self, obligations, matches):
        self.obligations = obligations
        self.matches = matches
        self.updates = []

    def table(self, name):
        return _Query(self, name)


def _obligation(**overrides):
    base = {
        "candidate_id": "sports_mlb_1",
        "event_id": "mlb_123",
        "selected_team": "New York Yankees",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "match_id": "match-1",
        "game_pk": 123,
        "shares": 10.0,
        "stake": 6.0,
        "settlement_status": "pending",
        "metadata": {"strategy": "mlb_moneyline"},
    }
    base.update(overrides)
    return base


def _match(**overrides):
    base = {
        "id": "match-1",
        "external_id": "mlb_123",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "scheduled_at": "2026-07-23T23:00:00+00:00",
        "winner": "New York Yankees",
        "is_final": True,
        "home_score": 5,
        "away_score": 2,
    }
    base.update(overrides)
    return base


class TestSportsShadowOutcomes(unittest.TestCase):
    def test_exact_game_pk_settles_phase4(self):
        db = _DB([_obligation()], [_match()])
        summary = settle_pending(
            db,
            now=datetime(2026, 7, 24, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["settled"], 1)
        self.assertEqual(db.updates[0]["settlement_status"], "won")
        self.assertEqual(db.updates[0]["settlement_pnl"], 4.0)

    def test_wrong_game_pk_fails_closed(self):
        db = _DB([_obligation(game_pk=999)], [_match()])
        summary = settle_pending(db)
        self.assertEqual(summary["settled"], 0)
        self.assertEqual(
            summary["failures"][SHADOW_SETTLEMENT_GAME_PK_MISMATCH],
            1,
        )
        self.assertEqual(db.updates, [])

    def test_model_probability_survives_durable_upsert(self):
        from pavlov.pipeline.clv_tracker import CLVRecord, _upsert_clv_obligation

        db = MagicMock()
        db.table.return_value.upsert.return_value.execute.return_value = MagicMock()
        record = CLVRecord(
            trade_id="sports_mlb_1",
            market_id="market-1",
            outcome_id="token-1",
            side="YES",
            entry_time=datetime.now(timezone.utc),
            entry_market_price=0.55,
            entry_effective_cost=0.58,
        )
        metadata = {
            "event_id": "mlb_123",
            "event_start": "2026-07-23T23:00:00+00:00",
            "model_prob": 0.64,
            "market_prob": 0.55,
            "selected_team": "New York Yankees",
            "home_team": "New York Yankees",
            "away_team": "Boston Red Sox",
            "match_id": "match-1",
            "game_pk": 123,
            "shares": 10.0,
            "stake": 5.8,
        }
        with patch("backend.db.get_db", return_value=db):
            _upsert_clv_obligation(record, metadata=metadata)
        row = db.table.return_value.upsert.call_args[0][0]
        self.assertEqual(row["model_prob"], 0.64)
        self.assertEqual(row["match_id"], "match-1")
        self.assertEqual(row["game_pk"], 123)

    def test_brier_and_log_loss_are_side_correct(self):
        summary = summarize_settled_outcomes(
            [
                {
                    "settlement_status": "won",
                    "settlement_result": True,
                    "model_prob": 0.8,
                    "market_prob": 0.6,
                },
                {
                    "settlement_status": "lost",
                    "settlement_result": False,
                    "model_prob": 0.2,
                    "market_prob": 0.4,
                },
            ]
        )
        self.assertEqual(summary["settled_model_n"], 2)
        self.assertAlmostEqual(summary["model_brier"], 0.04)
        self.assertAlmostEqual(summary["market_brier"], 0.16)
        self.assertLess(summary["model_log_loss"], summary["market_log_loss"])

    def test_no_settled_observations_returns_none(self):
        summary = summarize_settled_outcomes([])
        self.assertEqual(summary["settled_model_n"], 0)
        self.assertIsNone(summary["model_brier"])
        self.assertIsNone(summary["model_log_loss"])


if __name__ == "__main__":
    unittest.main()
