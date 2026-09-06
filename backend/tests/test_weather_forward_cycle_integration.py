"""Exercise the actual weather sync with public fixtures and a durable memory ledger."""
from contextlib import ExitStack, chdir
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from test_weather_experiment import MemoryDB, MemoryQuery
from backend.trading.weather_experiment import read_config

NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)  # 08:00 local at both fixture stations.


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW.timestamp(), tz=tz or timezone.utc)


class CycleQuery(MemoryQuery):
    def insert(self, payload):
        self.inserting = payload if isinstance(payload, list) else [payload]
        return self

    def delete(self):
        self.deleting = True
        return self

    def execute(self):
        if self.payload is not None and self.db.fail_snapshots and self.name == "model_predictions":
            raise RuntimeError("synthetic durable snapshot failure")
        if hasattr(self, "inserting"):
            rows = self.db.rows.setdefault(self.name, [])
            for row in self.inserting:
                row = deepcopy(row)
                row.setdefault("id", str(uuid4()))
                rows.append(row)
            return SimpleNamespace(data=deepcopy(self.inserting))
        if getattr(self, "deleting", False):
            selected = super().execute().data
            ids = {row["id"] for row in selected}
            self.db.rows[self.name] = [row for row in self.db.rows.get(self.name, []) if row["id"] not in ids]
            return SimpleNamespace(data=selected)
        return super().execute()


class CycleDB(MemoryDB):
    def __init__(self, *, fail_snapshots=False):
        super().__init__()
        self.fail_snapshots = fail_snapshots

    def table(self, name):
        return CycleQuery(self, name)


def market_rows(venue, city):
    rows = []
    for suffix, strike_type, label, ask in (("low", "less", "<80", 0.8), ("high", "greater", "80 or above", 0.2)):
        rows.append({
            "ticker": f"{venue}-{city.replace(' ', '')}-{suffix}",
            "title": f"{city} high temperature {label}", "city_hint": city,
            "market_date": "2026-09-06", "metric_hint": "high",
            "strike_type": strike_type, "floor_strike": 80, "cap_strike": 80,
            "best_bid": ask - 0.02, "best_ask": ask, "ask_size": 100,
            "received_timestamp": NOW.isoformat(), "orderbook_timestamp": None,
            "execution_price_source": "orderbook", "close_time": "2026-09-07T04:00:00Z",
            "fee_type": "quadratic", "fee_multiplier": 1,
            "fee_source": "synthetic_public_fee_schedule",
        })
    return rows


class TestWeatherForwardCycleIntegration(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, fail_snapshots=False):
        from backend.models.weather import sync_weather as sync
        from pavlov.pipeline import kalshi_client, order_simulator, clv_tracker
        from pavlov.polymarket import poly_client
        import backend.ml.weather_mos as weather_mos

        db = CycleDB(fail_snapshots=fail_snapshots)
        config = read_config()
        manifest = {"id": config["id"], "config": config, "config_hash": "integration-test-frozen"}
        markets = {"kalshi": market_rows("kalshi", "New York"), "polymarket": market_rows("polymarket", "Miami")}
        by_id = {row["ticker"]: row for rows in markets.values() for row in rows}
        # This fixture deliberately supplies an allowed cohort, so the real
        # normalization, distributions, optimizer, risk limits and fill simulator run.
        calibrator = MagicMock()
        calibrator.calibrate.side_effect = lambda **kw: SimpleNamespace(
            execution_probability=kw["probability"], allowed=True, reason=None,
            as_metadata=lambda: {"execution_calibration_allowed": True, "execution_training_as_of": NOW.isoformat()},
        )
        mos = SimpleNamespace(bias_correction=0, residual_sigma=4, source="integration_fixture",
                              station_samples=100, pooled_samples=100, training_as_of=NOW.isoformat())
        with tempfile.TemporaryDirectory() as directory, chdir(directory), ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"LIVE_TRADING_ENABLED": "true", "POLYMARKET_LIVE_ENABLED": "true", "MODE": "live"}))
            stack.enter_context(patch.object(sync, "datetime", FixedDatetime))
            stack.enter_context(patch.object(order_simulator, "datetime", FixedDatetime))
            stack.enter_context(patch.object(sync, "get_db", return_value=db))
            stack.enter_context(patch.object(sync, "load_weather_execution_calibrator", return_value=calibrator))
            stack.enter_context(patch.object(sync.ensemble_client, "get_ensemble_prob", return_value={"mean_f": 90, "spread_f": 2}))
            stack.enter_context(patch.object(sync, "get_current_obs", return_value={}))
            stack.enter_context(patch.object(weather_mos.mos_engine, "calculate_calibration", return_value=mos))
            stack.enter_context(patch("backend.ml.weather_verification.record_prediction", return_value=True))
            stack.enter_context(patch.object(poly_client, "get_weather_markets", side_effect=lambda: deepcopy(markets["polymarket"])))
            stack.enter_context(patch.object(kalshi_client, "get_weather_markets", side_effect=lambda: deepcopy(markets["kalshi"])))
            stack.enter_context(patch.object(poly_client, "get_orderbook_as_parsed", side_effect=lambda key: deepcopy(by_id[key])))
            stack.enter_context(patch.object(kalshi_client, "get_orderbook_as_parsed", side_effect=lambda key: deepcopy(by_id[key])))
            signed = [stack.enter_context(patch.object(module, method, side_effect=AssertionError("signed operation prohibited")))
                      for module, method in ((poly_client, "get_client"), (poly_client, "place_order"),
                                             (kalshi_client, "get_account_balance"), (kalshi_client, "place_order"))]
            simulator = stack.enter_context(patch.object(order_simulator, "simulate_paper_fill", wraps=order_simulator.simulate_paper_fill))
            stack.enter_context(patch.object(sync, "init_weather_clv_record", return_value=SimpleNamespace()))
            stack.enter_context(patch.object(clv_tracker, "log_clv_record"))
            stats = await sync.sync_weather_predictions(experiment=manifest, run_id="integration-run")
            first_snapshots = deepcopy([row for row in db.rows.get("model_predictions", []) if row["source"] == config["prediction_source"]])
            if not fail_snapshots:
                # A retry must retain the immutable decision even though its
                # changed account context can make the new write conflict.
                await sync.sync_weather_predictions(experiment=manifest, run_id="integration-run")
                snapshots_after_retry = [row for row in db.rows["model_predictions"] if row["source"] == config["prediction_source"]]
                self.assertEqual(first_snapshots, snapshots_after_retry)
            for operation in signed:
                operation.assert_not_called()
            self.assertTrue(all(call.kwargs["mode"] == "paper" for call in simulator.call_args_list))
            fill_calls = simulator.call_count
            fill_file_exists = Path("paper_fills.jsonl").exists()
        return db, stats, first_snapshots, fill_calls, fill_file_exists

    async def test_both_venues_record_immutable_forecasts_and_only_simulate_paper_fills(self):
        db, stats, snapshots, fill_calls, _ = await self.exercise()
        self.assertEqual(stats["events"], 2)
        self.assertEqual(stats["evaluation_rows"], 4)
        self.assertEqual(len(snapshots), 4)
        self.assertEqual({row["metadata"]["platform"] for row in snapshots}, {"kalshi", "polymarket"})
        self.assertTrue(all(row["metadata"]["decision_fingerprint"] for row in snapshots))
        self.assertEqual(stats["mode"], "paper")
        self.assertEqual(stats["bets_placed"], 2)
        self.assertEqual(fill_calls, 2)
        self.assertEqual({row["venue"] for row in db.rows["autobets"]}, {"kalshi", "polymarket"})
        self.assertTrue(all(row["mode"] == "paper" for row in db.rows["autobets"]))

    async def test_durable_snapshot_failure_prevents_fill_simulation_and_ledger_inserts(self):
        db, stats, snapshots, fill_calls, fill_file_exists = await self.exercise(fail_snapshots=True)
        self.assertEqual(stats["bets_placed"], 0)
        self.assertEqual(stats.get("persistence_reject"), 2)
        self.assertEqual(snapshots, [])
        self.assertEqual(db.rows.get("autobets", []), [])
        self.assertEqual(fill_calls, 0)
        self.assertFalse(fill_file_exists)


if __name__ == "__main__":
    unittest.main()
