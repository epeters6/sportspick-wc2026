"""Financial regressions using a paged, projected, mutable in-memory ledger."""
from __future__ import annotations

import copy
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.trading.autobet_learning import (
    invalidate_learning_cache,
    settlement_integrity_datasets,
)
from backend.trading.settlement_integrity import WEATHER_SETTLEMENT_VERSION
from backend.trading.weather_settlement import _apply_resolution, _grade_bet_against_venue
from scripts.reconcile_weather_settlements import reconcile_weather_settlements


class LedgerQuery:
    def __init__(self, db):
        self.db = db
        self.predicates = []
        self.columns = "*"
        self.cap = 1000
        self.payload = None

    def select(self, columns):
        self.columns = columns
        self.db.projections.append(columns)
        return self

    def eq(self, key, value):
        self.predicates.append(lambda row: row.get(key) == value)
        return self

    def is_(self, key, value):
        assert value == "null"
        return self.eq(key, None)

    def in_(self, key, values):
        self.predicates.append(lambda row: row.get(key) in values)
        return self

    def gt(self, key, value):
        self.predicates.append(lambda row: row[key] > value)
        return self

    def lte(self, key, value):
        self.predicates.append(lambda row: row[key] <= value)
        return self

    def order(self, key):
        assert key == "id"
        return self

    def limit(self, cap):
        self.cap = cap
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def execute(self):
        rows = [row for row in self.db.rows if all(test(row) for test in self.predicates)]
        rows.sort(key=lambda row: row["id"])
        rows = rows[:min(self.cap, self.db.server_cap)]
        if self.payload is not None:
            self.db.writes += len(rows)
            for row in rows:
                row.update(copy.deepcopy(self.payload))
        elif self.columns != "*":
            keys = {key.strip() for key in self.columns.split(",")}
            rows = [{key: value for key, value in row.items() if key in keys} for row in rows]
        return SimpleNamespace(data=copy.deepcopy(rows))


class LedgerDB:
    def __init__(self, rows, server_cap=37):
        self.rows = copy.deepcopy(rows)
        self.server_cap = server_cap
        self.writes = 0
        self.projections = []

    def table(self, table):
        assert table == "autobets"
        return LedgerQuery(self)


def old_bet(index=0, venue="kalshi"):
    market_id = "KXHIGHCHI-26AUG27-B82.5" if venue == "kalshi" else "tc-temp-mdwhigh-2026-08-27-gte82f"
    return {
        "id": f"bet-{index:05d}", "market_id": market_id,
        "sport": "weather", "bet_type": "weather", "mode": "paper",
        "outcome_name": "yes", "status": "won", "pnl": 147.98,
        "stake": 3.98, "shares": 151, "market_price": 0.025,
        "created_at": "2026-08-27T12:00:00Z", "resolved_at": "2026-08-28T12:00:00Z",
        "settlement_version": None, "settlement_corrected_at": None,
        "metadata": {"station": "KMDW", "resolution_source": "station_actual", "settlement": {"version": "weather_actual_v2", "actual_temp_f": 83}},
    }


async def official(market_id):
    venue = "kalshi" if market_id.startswith("KX") else "polymarket"
    return {"resolved": True, "winner": "no", "market_id": market_id, "venue": venue}


class TestWeatherReconciliation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        invalidate_learning_cache()

    async def asyncTearDown(self):
        invalidate_learning_cache()

    async def test_default_audits_beyond_500_with_server_capped_pages_and_no_writes(self):
        rows = [old_bet(i, "kalshi" if i % 2 else "polymarket") for i in range(603)]
        db = LedgerDB(rows)
        with patch("scripts.reconcile_weather_settlements.get_db", return_value=db), patch(
            "scripts.reconcile_weather_settlements.check_kalshi_resolution", side_effect=official
        ) as kalshi, patch(
            "scripts.reconcile_weather_settlements.check_polymarket_resolution", side_effect=official
        ) as poly, redirect_stdout(io.StringIO()):
            result = await reconcile_weather_settlements()
        self.assertEqual(result["reviewed"], 603)
        self.assertEqual(result["changes"], 603)
        self.assertTrue(result["complete"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(db.writes, 0)
        self.assertEqual(db.rows, rows)
        self.assertEqual(kalshi.await_count, 1)
        self.assertEqual(poly.await_count, 1)
        self.assertEqual(result["proposed"][0]["official"]["pnl"], -3.98)

    async def test_apply_preserves_history_and_repeat_is_idempotent(self):
        original = old_bet()
        db = LedgerDB([original])
        with patch("scripts.reconcile_weather_settlements.get_db", return_value=db), patch(
            "scripts.reconcile_weather_settlements.check_kalshi_resolution", side_effect=official
        ), redirect_stdout(io.StringIO()):
            first = await reconcile_weather_settlements(apply=True)
            second = await reconcile_weather_settlements(apply=True)
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["changes"], 0)
        self.assertEqual(db.writes, 1)
        row = db.rows[0]
        self.assertEqual(row["pnl"], -3.98)
        self.assertEqual(row["resolved_at"], original["resolved_at"])
        self.assertEqual(row["settlement_version"], WEATHER_SETTLEMENT_VERSION)
        history = row["metadata"]["settlement_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["pnl"], 147.98)
        self.assertEqual(history[0]["settlement"], original["metadata"]["settlement"])
        self.assertEqual(row["metadata"]["station"], "KMDW")

    async def test_unresolved_does_not_overwrite_even_a_current_version_row(self):
        row = old_bet()
        row["settlement_version"] = WEATHER_SETTLEMENT_VERSION
        db = LedgerDB([row])
        with patch("scripts.reconcile_weather_settlements.get_db", return_value=db), patch(
            "scripts.reconcile_weather_settlements.check_kalshi_resolution", new=AsyncMock(return_value=None)
        ):
            result = await reconcile_weather_settlements(apply=True)
        self.assertEqual(result["unresolved"], 1)
        self.assertEqual(db.rows, [row])
        self.assertEqual(db.writes, 0)

    async def test_explicit_limit_reports_partial_history(self):
        db = LedgerDB([old_bet(i) for i in range(4)])
        with patch("scripts.reconcile_weather_settlements.get_db", return_value=db), patch(
            "scripts.reconcile_weather_settlements.check_kalshi_resolution", side_effect=official
        ), redirect_stdout(io.StringIO()):
            result = await reconcile_weather_settlements(limit=2)
        self.assertEqual(result["reviewed"], 2)
        self.assertFalse(result["complete"])

    async def test_learning_receives_market_identity_and_all_server_pages(self):
        db = LedgerDB([old_bet(i) for i in range(4)], server_cap=1)
        with patch("scripts.reconcile_weather_settlements.get_db", return_value=db), patch(
            "scripts.reconcile_weather_settlements.check_kalshi_resolution", side_effect=official
        ), redirect_stdout(io.StringIO()):
            await reconcile_weather_settlements(apply=True)
        datasets = settlement_integrity_datasets(db)
        self.assertEqual(len(datasets["verified_rows"]), 4)
        self.assertEqual(datasets["unverifiable_rows"], [])
        self.assertTrue(all(row["market_id"] for row in datasets["verified_rows"]))

    async def test_current_version_does_not_hide_wrong_payout(self):
        row = old_bet()
        row["settlement_version"] = WEATHER_SETTLEMENT_VERSION
        db = LedgerDB([row])
        with patch("scripts.reconcile_weather_settlements.get_db", return_value=db), patch(
            "scripts.reconcile_weather_settlements.check_kalshi_resolution", side_effect=official
        ), redirect_stdout(io.StringIO()):
            result = await reconcile_weather_settlements(apply=True)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(db.rows[0]["pnl"], -3.98)

    async def test_stale_correction_cannot_overwrite_concurrently_changed_row(self):
        original = old_bet()
        db = LedgerDB([{**original, "pnl": 10}])
        graded = _grade_bet_against_venue(original, await official(original["market_id"]), "kalshi")
        result = _apply_resolution(
            db, original, graded["status"], graded["pnl"],
            datetime.now(timezone.utc), "reconciled:kalshi",
            settlement_evidence=graded, correction=True,
        )
        self.assertFalse(result)
        self.assertEqual(db.writes, 0)
        self.assertEqual(db.rows[0]["pnl"], 10)


if __name__ == "__main__":
    unittest.main()
