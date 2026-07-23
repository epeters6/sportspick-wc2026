from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend.trading.settlement_integrity import (
    INVALID_MATCH_WINNER,
    PRESTART_SETTLEMENT_BLOCK,
    verify_match_linked_autobet,
)


def _match(**overrides):
    base = {
        "id": "match-today",
        "sport": "mlb",
        "external_id": "mlb_123",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "scheduled_at": "2026-07-23T23:00:00+00:00",
        "finished_at": "2026-07-24T02:00:00+00:00",
        "winner": "New York Yankees",
        "is_final": True,
        "home_score": 5,
        "away_score": 2,
        "match_stats": {},
    }
    base.update(overrides)
    return base


def _bet(**overrides):
    base = {
        "id": "autobet-1",
        "match_id": "match-today",
        "sport": "mlb",
        "outcome_name": "New York Yankees",
        "bet_type": "moneyline",
        "bet_line": None,
        "bet_subject": None,
        "stake": 4.0,
        "shares": 8.0,
        "market_price": 0.5,
        "status": "won",
        "pnl": 4.0,
        "resolved_at": "2026-07-24T02:01:00+00:00",
    }
    base.update(overrides)
    return base


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self.filters = {}
        self.payload = None

    @property
    def not_(self):
        return self

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def in_(self, key, value):
        self.filters[key] = value
        return self

    def is_(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def execute(self):
        class Result:
            data = []

        result = Result()
        if self.payload is not None:
            self.db.updates.append((self.table_name, self.filters.copy(), self.payload))
            return result
        if self.table_name == "matches":
            wanted = set(self.filters.get("id", []))
            result.data = [m for m in self.db.matches if m["id"] in wanted]
        elif self.table_name == "autobets":
            if self.filters.get("status") == "open":
                result.data = self.db.open_bets
            elif self.filters.get("status") == ["won", "lost"]:
                result.data = self.db.settled_bets
        return result


class _DB:
    def __init__(self, *, matches, open_bets=None, settled_bets=None):
        self.matches = matches
        self.open_bets = open_bets or []
        self.settled_bets = settled_bets or []
        self.updates = []

    def table(self, name):
        return _Query(self, name)


class TestSettlementIntegrity(unittest.TestCase):
    def test_exact_linked_game_is_valid(self):
        check = verify_match_linked_autobet(
            _bet(),
            _match(),
            now=datetime(2026, 7, 24, 3, tzinfo=timezone.utc),
        )
        self.assertTrue(check.valid)
        self.assertEqual(check.expected_status, "won")
        self.assertEqual(check.expected_pnl, 4.0)

    def test_final_looking_data_before_start_is_rejected(self):
        check = verify_match_linked_autobet(
            _bet(resolved_at=None),
            _match(),
            now=datetime(2026, 7, 23, 22, tzinfo=timezone.utc),
        )
        self.assertFalse(check.valid)
        self.assertEqual(check.reason, PRESTART_SETTLEMENT_BLOCK)

    def test_historical_prestart_resolution_is_valid_after_exact_reverification(self):
        check = verify_match_linked_autobet(
            _bet(
                resolved_at="2026-07-23T21:00:00+00:00",
                settlement_version="exact_match_v2",
                settlement_match_id="match-today",
                settlement_corrected_at="2026-07-24T03:00:00+00:00",
            ),
            _match(),
            now=datetime(2026, 7, 24, 4, tzinfo=timezone.utc),
        )

        self.assertTrue(check.valid)
        self.assertIsNone(check.reason)

    def test_invalid_winner_is_unresolved_not_loss(self):
        check = verify_match_linked_autobet(
            _bet(status="lost", pnl=-4.0),
            _match(winner="TBD"),
            now=datetime(2026, 7, 24, 3, tzinfo=timezone.utc),
        )
        self.assertFalse(check.valid)
        self.assertEqual(check.reason, INVALID_MATCH_WINNER)
        self.assertIsNone(check.expected_status)

    def test_same_teams_yesterday_does_not_settle_today(self):
        today = _match(is_final=False, winner=None)
        yesterday = _match(
            id="match-yesterday",
            is_final=True,
            winner="New York Yankees",
        )
        db = _DB(
            matches=[today, yesterday],
            open_bets=[_bet(status="open", pnl=None, resolved_at=None)],
        )
        with patch("backend.trading.autobet.get_db", return_value=db):
            from backend.trading.autobet import resolve_autobets

            self.assertEqual(resolve_autobets(), 0)
        self.assertEqual(db.updates, [])

    def test_doubleheader_game_two_remains_open(self):
        game_one = _match(id="game-one", external_id="mlb_1")
        game_two = _match(
            id="game-two",
            external_id="mlb_2",
            is_final=False,
            winner=None,
        )
        db = _DB(
            matches=[game_one, game_two],
            open_bets=[
                _bet(
                    match_id="game-two",
                    status="open",
                    pnl=None,
                    resolved_at=None,
                )
            ],
        )
        with patch("backend.trading.autobet.get_db", return_value=db):
            from backend.trading.autobet import resolve_autobets

            self.assertEqual(resolve_autobets(), 0)
        self.assertEqual(db.updates, [])

    def test_historical_correction_preserves_resolved_at(self):
        historical_time = "2026-07-24T02:01:00+00:00"
        db = _DB(
            matches=[_match(scheduled_at="2026-07-22T23:00:00+00:00")],
            settled_bets=[
                _bet(status="lost", pnl=-4.0, resolved_at=historical_time)
            ],
        )
        with patch("backend.trading.autobet.get_db", return_value=db):
            from backend.trading.autobet import resolve_autobets

            self.assertEqual(resolve_autobets(), 1)
        payload = db.updates[0][2]
        self.assertNotIn("resolved_at", payload)
        self.assertIn("settlement_corrected_at", payload)
        self.assertEqual(payload["status"], "won")


if __name__ == "__main__":
    unittest.main()
