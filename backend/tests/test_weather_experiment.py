from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.trading.weather_experiment import (
    account_state, entry_budget, event_scope_reason, fetch_experiment_bets,
    read_config, register_experiment, save_prediction_snapshot,
)


class MemoryQuery:
    def __init__(self, db, table):
        self.db, self.name = db, table
        self.filters, self.payload, self.ignore = [], None, False
        self.maximum, self.sort_key = 1000, None

    def select(self, *args, **kwargs):
        return self

    def eq(self, key, value):
        self.filters.append((key, "eq", value))
        return self

    def gt(self, key, value):
        self.filters.append((key, "gt", value))
        return self

    def in_(self, key, value):
        self.filters.append((key, "in", value))
        return self

    def order(self, key, **kwargs):
        self.sort_key = key
        return self

    def limit(self, value):
        self.maximum = value
        return self

    def upsert(self, payload, *, on_conflict="id", ignore_duplicates=False):
        self.payload, self.conflict, self.ignore = payload, on_conflict, ignore_duplicates
        return self

    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        if self.payload is not None:
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            for item in payload:
                old = next((r for r in rows if r[self.conflict] == item[self.conflict]), None)
                if old is None:
                    rows.append(copy.deepcopy(item))
                elif not self.ignore:
                    old.update(copy.deepcopy(item))
            return SimpleNamespace(data=copy.deepcopy(payload))
        selected = []
        for row in rows:
            good = True
            for key, operator, expected in self.filters:
                if "->>" in key:
                    parent, child = key.split("->>")
                    value = row.get(parent, {}).get(child)
                else:
                    value = row.get(key)
                good &= (value == expected if operator == "eq" else
                         value in expected if operator == "in" else value > expected)
            if good:
                selected.append(row)
        if self.sort_key:
            selected.sort(key=lambda r: r[self.sort_key])
        return SimpleNamespace(data=copy.deepcopy(selected[:min(self.maximum, self.db.page_cap)]))


class MemoryDB:
    def __init__(self, rows=None, page_cap=1000):
        self.rows, self.page_cap = rows or {}, page_cap

    def table(self, name):
        return MemoryQuery(self, name)


def paper_bet(config, **overrides):
    row = {"id": "a", "mode": "paper", "venue": "kalshi", "sport": "weather",
           "market_id": "KXHIGHNY-26SEP05-B80.5", "outcome_name": "yes",
           "stake": 2.0, "shares": 4.0, "market_price": 0.49,
           "created_at": "2026-09-05T12:00:00+00:00", "resolved_at": None,
           "status": "open", "pnl": None, "metadata": {"experiment_id": config["id"]}}
    row.update(overrides)
    return row


def settled_bet(config, **overrides):
    row = paper_bet(config, status="won", pnl=2.0, resolved_at="2026-09-06T08:00:00+00:00")
    row["metadata"]["settlement"] = {
        "version": "weather_venue_official_v3", "source": "kalshi_official",
        "market_id": row["market_id"], "official_result": "yes",
    }
    row.update(overrides)
    return row


class TestWeatherExperiment(unittest.TestCase):
    def test_paper_receipt_clock_never_validates_live_or_future_books(self):
        from pavlov.pipeline.order_simulator import validate_orderbook_freshness
        now = datetime.now(timezone.utc)
        validate_orderbook_freshness(None, now, mode="paper", allow_received_timestamp_for_shadow=True)
        with self.assertRaisesRegex(ValueError, "MISSING_ORDERBOOK_TIMESTAMP"):
            validate_orderbook_freshness(None, now, mode="live", allow_received_timestamp_for_shadow=True)
        with self.assertRaisesRegex(ValueError, "FUTURE_ORDERBOOK_TIMESTAMP"):
            validate_orderbook_freshness(None, now + timedelta(hours=1), mode="paper", allow_received_timestamp_for_shadow=True)
        with self.assertRaisesRegex(ValueError, "FUTURE_ORDERBOOK_TIMESTAMP"):
            validate_orderbook_freshness(now + timedelta(hours=1), now, mode="live")

    def setUp(self):
        self.config = read_config()
        self.now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)

    def test_accounts_reserve_open_cost_and_do_not_mix_legacy_or_other_venue(self):
        rows = [paper_bet(self.config), paper_bet(self.config, venue="polymarket", stake=100),
                paper_bet(self.config, metadata={"experiment_id": "legacy"}, stake=400)]
        account = account_state(rows, self.config, "kalshi", now=self.now)
        self.assertEqual(account["available_cash"], 498)
        self.assertEqual(account["reserved"], 2)
        self.assertEqual(entry_budget(account, self.config), 2.5)

    def test_verified_profit_is_separate_from_open_cash(self):
        account = account_state([settled_bet(self.config), paper_bet(self.config, id="b")], self.config, "kalshi", now=self.now)
        self.assertEqual(account["equity"], 502)
        self.assertEqual(account["available_cash"], 500)

    def test_bad_or_future_settlement_cannot_fund_new_trade(self):
        for row in (settled_bet(self.config, pnl=200),
                    settled_bet(self.config, resolved_at="2026-09-07T08:00:00+00:00"),
                    settled_bet(self.config, pnl=float("nan"))):
            account = account_state([row], self.config, "kalshi", now=self.now)
            self.assertEqual(account["quarantined"], 1)
            self.assertEqual(account["realized_pnl"], 0)
            self.assertEqual(entry_budget(account, self.config), 0)

    def test_wrong_official_venue_is_quarantined(self):
        row = settled_bet(self.config)
        row["metadata"]["settlement"]["source"] = "polymarket_official"
        self.assertEqual(account_state([row], self.config, "kalshi", now=self.now)["quarantined"], 1)

    def test_unknown_venue_blocks_both_accounts(self):
        for venue in self.config["venues"]:
            state = account_state([paper_bet(self.config, venue="unknown")], self.config, venue, now=self.now)
            self.assertEqual(state["quarantined"], 1)
            self.assertEqual(entry_budget(state, self.config), 0)

    def test_open_risk_and_daily_loss_limits(self):
        account = account_state([paper_bet(self.config, stake=25)], self.config, "kalshi", now=self.now)
        self.assertEqual(entry_budget(account, self.config), 0)
        loss = settled_bet(self.config, status="lost", stake=10, pnl=-10)
        loss["metadata"]["settlement"]["official_result"] = "no"
        account = account_state([loss], self.config, "kalshi", now=self.now)
        self.assertIn("DAILY_LOSS_LIMIT", account["blocked_reasons"])

    def test_server_page_cap_does_not_truncate_account_history(self):
        rows = [paper_bet(self.config, id=str(i)) for i in range(7)]
        db = MemoryDB({"autobets": rows}, page_cap=2)
        self.assertEqual(len(fetch_experiment_bets(db, self.config["id"], page_size=500)), 7)

    def test_manifest_cannot_be_retuned_under_same_id(self):
        db = MemoryDB()
        with patch("backend.trading.weather_experiment.implementation_hash", return_value="fixed"):
            original = register_experiment(db, config=self.config)
            self.assertEqual(register_experiment(db, config=self.config), original)
            changed = {**self.config, "min_net_edge": 0.01}
            with self.assertRaisesRegex(ValueError, "WEATHER_EXPERIMENT_CHANGED"):
                register_experiment(db, config=changed)
        self.assertEqual(db.rows["app_settings"][0]["value"]["config"]["min_net_edge"], 0.05)

    def test_snapshot_is_immutable_and_changed_retry_fails_before_execution(self):
        db = MemoryDB()
        manifest = {"id": self.config["id"], "config": self.config, "config_hash": "fixed"}
        args = {"manifest": manifest, "run_id": "one", "event_key": "weather:kalshi:KNYC:2026-09-06:high",
                "decision_at": self.now.isoformat(), "rows": [{"outcome": "market", "prob": 0.6, "market_price": 0.5, "metadata": {}}]}
        save_prediction_snapshot(db, **args)
        save_prediction_snapshot(db, **args)
        self.assertEqual(len(db.rows["model_predictions"]), 1)
        args["rows"][0]["prob"] = 0.9
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_CONFLICT"):
            save_prediction_snapshot(db, **args)
        self.assertEqual(db.rows["model_predictions"][0]["prob"], 0.6)

    def test_fixed_scope_and_local_hours(self):
        args = dict(venue="polymarket", station="KNYC", metric="high", lead_days=0, local_hour=8)
        self.assertIsNone(event_scope_reason(self.config, **args))
        self.assertEqual(event_scope_reason(self.config, **{**args, "local_hour": 9}), "OUTSIDE_WEATHER_DECISION_WINDOW")
        self.assertEqual(event_scope_reason(self.config, **{**args, "metric": "low"}), "OUTSIDE_WEATHER_EXPERIMENT_UNIVERSE")


class TestWeatherCycle(unittest.IsolatedAsyncioTestCase):
    async def test_import_failure_still_settles_and_persists_status(self):
        import importlib
        from scripts.run_weather_cycle import run_cycle
        config = read_config()
        manifest = {"id": config["id"], "config": config, "config_hash": "fixed"}
        calls = []
        real_import = importlib.import_module
        def import_stage(name):
            if name == "backend.models.weather.sync_weather":
                raise ImportError("broken forecast dependency")
            if name == "backend.trading.weather_settlement":
                return SimpleNamespace(resolve_weather_autobets=lambda: calls.append("settle"))
            if name == "backend.ml.weather_verification":
                return SimpleNamespace(backfill_actuals=lambda **kwargs: 0)
            if name == "backend.ml.prediction_evaluation":
                return SimpleNamespace(resolve_weather_prediction_backlog=lambda *args, **kwargs: 0)
            if name == "backend.trading.weather_forward_report":
                return SimpleNamespace(build_forward_report=lambda *args: {"live_ready": False})
            return real_import(name)
        with tempfile.TemporaryDirectory() as work, \
                patch("scripts.run_weather_cycle.register_experiment", return_value=manifest), \
                patch("scripts.run_weather_cycle.importlib", SimpleNamespace(import_module=import_stage)):
            report = await run_cycle(db=MemoryDB(), now=datetime(2026, 9, 6, 12, tzinfo=timezone.utc),
                                     report_path=Path(work) / "report.json")
        self.assertEqual(calls, ["settle", "settle"])
        self.assertEqual(report["stages"]["forecast"]["error"], "ImportError")
        self.assertEqual(report["status"], "degraded")

    async def test_settlement_and_reporting_survive_forecast_failure(self):
        from scripts.run_weather_cycle import run_cycle
        config = read_config()
        manifest = {"id": config["id"], "config": config, "config_hash": "fixed"}
        calls = []
        def failed_forecast():
            calls.append("forecast")
            raise RuntimeError("provider unavailable")
        functions = {"settlement_before": lambda: calls.append("before"), "forecast": failed_forecast,
                     "settlement_after": lambda: calls.append("after"), "forward_evaluation": lambda: {"live_ready": False}}
        with tempfile.TemporaryDirectory() as work, patch("scripts.run_weather_cycle.register_experiment", return_value=manifest):
            report = await run_cycle(db=MemoryDB(), stage_functions=functions,
                                     now=datetime(2026, 9, 6, 12, tzinfo=timezone.utc), report_path=Path(work) / "report.json")
            self.assertEqual(calls, ["before", "forecast", "after"])
            self.assertEqual(report["status"], "degraded")
            self.assertFalse(report["live_ready"])
            self.assertEqual(report["venues"]["polymarket"]["available_cash"], 500)
            self.assertEqual(json.loads((Path(work) / "report.json").read_text())["status"], "degraded")

    async def test_second_runner_cannot_take_another_forecast_in_same_hour(self):
        from scripts.run_weather_cycle import run_cycle
        config = read_config()
        manifest = {"id": config["id"], "config": config, "config_hash": "fixed"}
        calls, db = [], MemoryDB()
        functions = {"forecast": lambda: calls.append("forecast")}
        with tempfile.TemporaryDirectory() as work, patch("scripts.run_weather_cycle.register_experiment", return_value=manifest):
            for _ in range(2):
                await run_cycle(db=db, stage_functions=functions, now=datetime(2026, 9, 6, 12, tzinfo=timezone.utc),
                                report_path=Path(work) / "report.json")
        self.assertEqual(calls, ["forecast"])

    async def test_failed_forecast_retains_claim_and_blocks_same_hour_retry(self):
        from scripts.run_weather_cycle import run_cycle

        config = read_config()
        manifest = {"id": config["id"], "config": config, "config_hash": "fixed"}
        calls, db = [], MemoryDB()
        started = datetime(2026, 9, 6, 12, 7, tzinfo=timezone.utc)
        claim_key = f"weather_forecast_slot:{config['id']}:20260906T12"

        def failed_forecast():
            calls.append("failed_attempt")
            raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as work, \
                patch("scripts.run_weather_cycle.register_experiment", return_value=manifest):
            first = await run_cycle(
                db=db, stage_functions={"forecast": failed_forecast}, now=started,
                report_path=Path(work) / "first.json",
            )
            original_claim = copy.deepcopy(next(
                row for row in db.rows["app_settings"] if row["key"] == claim_key
            ))
            second = await run_cycle(
                db=db, stage_functions={"forecast": lambda: calls.append("retry")},
                now=started + timedelta(minutes=20),
                report_path=Path(work) / "second.json",
            )

        self.assertEqual(first["stages"]["forecast"], {"status": "failed", "error": "RuntimeError"})
        self.assertEqual(second["stages"]["forecast"], {
            "status": "skipped", "reason": "FORECAST_SLOT_ALREADY_ATTEMPTED",
        })
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(calls, ["failed_attempt"])
        claims = [row for row in db.rows["app_settings"] if row["key"] == claim_key]
        self.assertEqual(claims, [original_claim])
        self.assertEqual(original_claim["value"], {
            "run_id": first["run_id"], "started_at": started.isoformat(),
        })
