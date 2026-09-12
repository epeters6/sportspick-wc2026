"""Public-fixture integration for independent weather paper research arms."""
from contextlib import ExitStack, chdir
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from test_weather_forward_cycle_integration import CycleDB, NOW, market_rows
from backend.trading.weather_research import read_research_configs, run_research_suite


class TestWeatherResearchSync(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, deny_calibration=False, expire_on_reprice=False,
                       invalid_partition=False, ensemble_mean=90):
        from backend.models.weather import sync_weather as sync
        from pavlov.pipeline import kalshi_client, order_simulator
        from pavlov.polymarket import poly_client
        import backend.ml.weather_mos as weather_mos

        configs = read_research_configs()
        if expire_on_reprice:
            configs = [configs[-1]]
        db = CycleDB()
        legacy = {"id": "legacy-training-sentinel", "source": "weather_calibrated_v2",
                  "domain": "weather", "event_key": "legacy-event",
                  "outcome": "legacy-market", "prob": 0.123, "metadata": {"legacy": True}}
        db.rows["model_predictions"] = [deepcopy(legacy)]
        markets = {"kalshi": market_rows("kalshi", "New York"),
                   "polymarket": market_rows("polymarket", "Miami")}
        if invalid_partition:
            # Overlap [78.5,79.5] and gap [100.5,101.5] have equal normal
            # mass around mean=90. A sum-to-one check alone misses this defect.
            for venue, rows in markets.items():
                base = rows[0]
                malformed = []
                for index, (title, strike, lo, hi) in enumerate((
                    ("<80", "less", None, 80),
                    ("between 79 and 89", "between", 79, 89),
                    ("between 90 and 100", "between", 90, 100),
                    ("102 or above", "greater", 102, None),
                )):
                    row = deepcopy(base)
                    row.update({"ticker": f"{base['ticker']}-partition-{index}",
                                "title": f"{base['city_hint']} high temperature {title}",
                                "strike_type": strike, "floor_strike": lo,
                                "ceiling_strike": hi, "threshold_lo": lo, "threshold_hi": hi})
                    malformed.append(row)
                markets[venue] = malformed
        for venue, rows in markets.items():
            station = "KNYC" if venue == "kalshi" else "KMIA"
            for row in rows:
                row["settlement_station"] = station
                # NWS daily climate reports use the fixed standard-time day.
                row["close_time"] = "2026-09-07T05:00:00Z"
                row["rules_primary"] = f"National Weather Service official daily climate report at {station}."
                if venue == "kalshi":
                    row["series_ticker"] = "KXHIGHNY"
                    row["ticker"] = "KXHIGHNY-26SEP06-" + row["ticker"].rsplit("-", 1)[1]
        by_id = {row["ticker"]: row for rows in markets.values() for row in rows}
        shared = {"forecast_hour": NOW.strftime("%Y%m%dT%H")}
        clock = {"value": NOW}

        class MovingDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(clock["value"].timestamp(), tz=tz or timezone.utc)

        calibrator = MagicMock()
        calibrator.calibrate.side_effect = lambda **kw: SimpleNamespace(
            execution_probability=0.05 if deny_calibration else kw["probability"],
            allowed=not deny_calibration,
            reason="INSUFFICIENT_HISTORICAL_COHORT" if deny_calibration else None,
            as_metadata=lambda: {
                "execution_calibration_allowed": not deny_calibration,
                "execution_training_as_of": NOW.isoformat(),
            },
        )
        mos = SimpleNamespace(bias_correction=0, residual_sigma=4, source="public_fixture",
                              station_samples=100, pooled_samples=100, training_as_of=NOW.isoformat())

        def fresh_book(key):
            book = deepcopy(by_id[key])
            # A changed winner price must affect only that arm's execution book.
            book["best_ask"] -= 0.01
            book["reprice_only_marker"] = True
            if expire_on_reprice:
                clock["value"] = NOW + timedelta(hours=1)
            return book

        with tempfile.TemporaryDirectory() as directory, chdir(directory), ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {
                "LIVE_TRADING_ENABLED": "true", "POLYMARKET_LIVE_ENABLED": "true", "MODE": "live"}))
            stack.enter_context(patch.object(sync, "datetime", MovingDatetime))
            stack.enter_context(patch.object(order_simulator, "datetime", MovingDatetime))
            stack.enter_context(patch.object(sync, "get_db", return_value=db))
            loader = stack.enter_context(patch.object(sync, "load_weather_execution_calibrator", return_value=calibrator))
            ensemble = stack.enter_context(patch.object(sync.ensemble_client, "get_ensemble_prob",
                                                       return_value={"mean_f": ensemble_mean, "spread_f": 2}))
            observation = stack.enter_context(patch.object(sync, "get_current_obs", side_effect=AssertionError("legacy station observation prohibited")))
            mos_mock = stack.enter_context(patch.object(weather_mos.mos_engine, "calculate_calibration", return_value=mos))
            verification = stack.enter_context(patch("backend.ml.weather_verification.record_prediction", return_value=True))
            discovery = [
                stack.enter_context(patch.object(module, "get_weather_markets",
                    side_effect=lambda venue=venue: deepcopy(markets[venue])))
                for module, venue in ((poly_client, "polymarket"), (kalshi_client, "kalshi"))
            ]
            books = [stack.enter_context(patch.object(module, "get_orderbook_as_parsed", side_effect=fresh_book))
                     for module in (poly_client, kalshi_client)]
            prohibited = ((poly_client, "get_client"), (poly_client, "get_account_balance"),
                          (poly_client, "place_order"), (kalshi_client, "_load_private_key"),
                          (kalshi_client, "_sign_request"), (kalshi_client, "_post"),
                          (kalshi_client, "get_account_balance"), (kalshi_client, "get_open_positions"),
                          (kalshi_client, "place_order"))
            signed = [stack.enter_context(patch.object(module, method,
                        side_effect=AssertionError("signed operation prohibited")))
                      for module, method in prohibited]
            simulator = stack.enter_context(patch.object(order_simulator, "simulate_paper_fill",
                                                          wraps=order_simulator.simulate_paper_fill))
            legacy_clv = stack.enter_context(patch.object(sync, "init_weather_clv_record",
                                                          side_effect=AssertionError("legacy CLV overwrite prohibited")))
            legacy_writer = stack.enter_context(patch.object(sync, "replace_prediction_rows",
                                                              side_effect=AssertionError("legacy prediction rewrite prohibited")))
            results, status_writes = {}, []
            stack.enter_context(patch.object(sync, "_write_weather_sync_status",
                                side_effect=lambda **kw: status_writes.append(deepcopy(kw))))
            for index, config in enumerate(configs):
                manifest = {"id": config["id"], "config": config,
                            "config_hash": f"frozen-arm-{index}"}
                if invalid_partition:
                    with self.assertRaisesRegex(RuntimeError, "ensemble returned data for 0"):
                        await sync.sync_weather_predictions(
                            experiment=manifest, run_id=f"arm-run-{index}", shared_inputs=shared)
                    results[config["id"]] = status_writes[-1]["stats"]
                else:
                    results[config["id"]] = await sync.sync_weather_predictions(
                        experiment=manifest, run_id=f"arm-run-{index}", shared_inputs=shared)
            for operation in signed:
                operation.assert_not_called()
            observation.assert_not_called()
            legacy_clv.assert_not_called()
            legacy_writer.assert_not_called()
            self.assertTrue(all(call.kwargs["mode"] == "paper" for call in simulator.call_args_list))
            metrics = {
                "ensemble": ensemble.call_count, "mos": mos_mock.call_count,
                "calibrator_loads": loader.call_count, "verification": verification.call_args_list,
                "discovery": [call.call_count for call in discovery],
                "books": sum(call.call_count for call in books),
                "fills": simulator.call_count,
            }
        self.assertEqual(next(row for row in db.rows["model_predictions"] if row["id"] == legacy["id"]), legacy)
        return configs, db, results, shared, metrics

    async def test_denied_calibration_blocks_required_arms_only(self):
        configs, db, results, _, metrics = await self.exercise(deny_calibration=True)
        for config in configs[:-1]:
            self.assertEqual(results[config["id"]]["bets_placed"], 0)
            self.assertEqual(results[config["id"]]["rejection_reasons"]["INSUFFICIENT_HISTORICAL_COHORT"], 2)
        self.assertEqual(results[configs[-1]["id"]]["bets_placed"], 2)
        self.assertEqual({bet["venue"] for bet in db.rows["autobets"]}, {"kalshi", "polymarket"})
        self.assertTrue(all(bet["metadata"]["experiment_id"] == configs[-1]["id"] for bet in db.rows["autobets"]))
        self.assertTrue(all(bet["metadata"]["calibration_gate_enforced"] is False for bet in db.rows["autobets"]))
        self.assertEqual(metrics["fills"], 2)
        self.assertEqual(len(db.rows["clv_obligations"]), 2)
        self.assertTrue(all("clv_obligation" in bet["metadata"] for bet in db.rows["autobets"]))

    async def test_all_arms_use_shared_inputs_with_isolated_ledger_and_clv_identities(self):
        configs, db, results, shared, metrics = await self.exercise()
        self.assertEqual([results[config["id"]]["bets_placed"] for config in configs], [2, 2, 2])
        snapshots = [row for row in db.rows["model_predictions"] if row["source"].startswith("weather_forward_")]
        bets, obligations = db.rows["autobets"], db.rows["clv_obligations"]
        self.assertEqual(len(snapshots), 12)
        self.assertEqual(len({row["id"] for row in snapshots}), 12)
        self.assertEqual(len({row["id"] for row in bets}), 6)
        self.assertEqual(len({row["candidate_id"] for row in obligations}), 6)
        by_fill = {row["candidate_id"]: row for row in obligations}
        snapshot_ids = {row["id"] for row in snapshots}
        for bet in bets:
            meta = bet["metadata"]
            self.assertIn(meta["decision_snapshot_id"], snapshot_ids)
            self.assertEqual(by_fill[f"weather-fill:{bet['id']}"], meta["clv_obligation"])
            self.assertEqual(meta["experiment_id"], by_fill[f"weather-fill:{bet['id']}"]["metadata"]["experiment_id"])
            self.assertEqual(bet["mode"], "paper")
            self.assertLessEqual(bet["stake"], 2.5)
        self.assertEqual(metrics["discovery"], [1, 1])
        self.assertEqual(metrics["ensemble"], 2)
        self.assertEqual(metrics["mos"], 2)
        self.assertEqual(metrics["calibrator_loads"], 2)
        self.assertEqual(metrics["books"], 6)
        self.assertEqual(len(metrics["verification"]), 2)
        self.assertTrue(all(call.kwargs["model_name"] == "ensemble_station_v2" for call in metrics["verification"]))
        self.assertTrue(all("reprice_only_marker" not in row for row in shared["market_capture"]["markets"]))
        self.assertEqual({row["metadata"]["input_capture_id"] for row in snapshots}, {shared["capture_id"]})
        # Forecast prices remain identical across arms despite independent reprices.
        for outcome in {row["outcome"] for row in snapshots}:
            rows = [row for row in snapshots if row["outcome"] == outcome]
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({row["market_price"] for row in rows}), 1)

    async def test_balanced_overlap_and_gap_are_rejected_before_forecasting(self):
        _, db, results, _, metrics = await self.exercise(invalid_partition=True)
        for stats in results.values():
            self.assertEqual(stats["rejection_reasons"]["INVALID_WEATHER_BUCKET_PARTITION"], 2)
            self.assertEqual(stats["bets_placed"], 0)
        self.assertEqual(metrics["ensemble"], 0)
        self.assertEqual(metrics["fills"], 0)
        self.assertEqual(metrics["books"], 0)
        self.assertEqual(db.rows.get("autobets", []), [])
        self.assertEqual(db.rows.get("clv_obligations", []), [])
        self.assertFalse(any(row["source"].startswith("weather_forward_")
                             for row in db.rows["model_predictions"]))

    async def test_unmet_minimum_edge_has_precise_reason_in_every_arm(self):
        _, db, results, _, metrics = await self.exercise(ensemble_mean=75.6)
        for stats in results.values():
            self.assertEqual(stats["rejection_reasons"]["NO_BUCKET_MEETS_MIN_NET_EDGE"], 2)
            self.assertNotIn("NON_POSITIVE_EXPECTED_LOG_GROWTH_AFTER_ROUNDING", stats["rejection_reasons"])
            self.assertEqual(stats["bets_placed"], 0)
        self.assertEqual(metrics["fills"], 0)
        self.assertEqual(metrics["books"], 0)
        self.assertEqual(db.rows.get("autobets", []), [])
        self.assertEqual(len([row for row in db.rows["model_predictions"]
                              if row["source"].startswith("weather_forward_")]), 12)

    async def test_hour_expiration_during_reprice_prevents_fill_and_ledger_write(self):
        _, db, results, _, metrics = await self.exercise(expire_on_reprice=True)
        stats = next(iter(results.values()))
        self.assertEqual(stats["bets_placed"], 0)
        self.assertGreaterEqual(stats["rejection_reasons"]["WEATHER_RESEARCH_HOUR_EXPIRED"], 1)
        self.assertEqual(metrics["fills"], 0)
        self.assertEqual(db.rows.get("autobets", []), [])
        self.assertEqual(db.rows.get("clv_obligations", []), [])


class TestWeatherResearchOrchestration(unittest.IsolatedAsyncioTestCase):
    async def test_suite_keeps_arm_pnl_separate_and_shares_only_input_context(self):
        configs, db, calls = read_research_configs(), CycleDB(), []
        pnls = [2, -3, 4]

        async def runner(**kwargs):
            calls.append(kwargs)
            index = len(calls) - 1
            config = kwargs["experiment_config"]
            return {"status": "healthy", "mode": "paper", "run_id": f"run-{index}",
                    "stages": {}, "experiment": {"id": config["id"], "config": config},
                    "venues": {"kalshi": {"realized_pnl": pnls[index], "equity": 500 + pnls[index]}}}

        with tempfile.TemporaryDirectory() as directory:
            report = await run_research_suite(db=db, configs=configs, now=NOW,
                cycle_runner=runner, report_path=Path(directory) / "latest.json")
        self.assertEqual(report["venues"]["kalshi"]["realized_pnl"], 2)
        self.assertEqual([report["experiments"][config["id"]]["venues"]["kalshi"]["realized_pnl"] for config in configs], pnls)
        self.assertEqual(report["active_experiment_ids"], [config["id"] for config in configs])
        self.assertEqual([call["include_global_stages"] for call in calls], [True, False, False])
        self.assertTrue(all(call["persist_latest"] is False for call in calls))
        self.assertTrue(all(call["shared_inputs"] is calls[0]["shared_inputs"] for call in calls))
        self.assertEqual(len({call["report_path"] for call in calls}), 3)
        self.assertFalse(report["live_ready"])
        saved = next(row for row in db.rows["app_settings"] if row["key"] == "weather_cycle_latest")
        self.assertEqual(saved["value"]["experiments"], report["experiments"])

    async def test_one_failed_arm_does_not_prevent_other_arm_reports(self):
        configs, calls = read_research_configs(), []

        async def runner(**kwargs):
            config = kwargs["experiment_config"]
            calls.append(config["id"])
            if config["id"] == configs[1]["id"]:
                raise RuntimeError("synthetic arm failure")
            return {"status": "healthy", "mode": "paper", "run_id": config["id"],
                    "stages": {}, "experiment": {"id": config["id"]}, "venues": {}}

        with tempfile.TemporaryDirectory() as directory:
            report = await run_research_suite(db=CycleDB(), configs=configs, now=NOW,
                cycle_runner=runner, report_path=Path(directory) / "latest.json")
        self.assertEqual(calls, [config["id"] for config in configs])
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["experiments"][configs[1]["id"]]["status"], "failed")
        self.assertEqual(report["experiments"][configs[2]["id"]]["status"], "healthy")
        self.assertEqual(report["stages"]["research_arms"]["status"], "failed")

    async def test_expired_suite_hour_never_claims_another_forecast_slot(self):
        from scripts import run_weather_cycle as cycle
        config, db = read_research_configs()[0], CycleDB()

        class ExpiredDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp((NOW + timedelta(hours=1)).timestamp(), tz=tz or timezone.utc)

        forecast = MagicMock(return_value={})
        with tempfile.TemporaryDirectory() as directory, patch.object(cycle, "datetime", ExpiredDatetime):
            report = await cycle.run_cycle(
                db=db, now=NOW, experiment_config=config,
                shared_inputs={"forecast_hour": NOW.strftime("%Y%m%dT%H")},
                stage_functions={"forecast": forecast}, include_global_stages=False,
                report_path=Path(directory) / "latest.json")
        forecast.assert_not_called()
        self.assertFalse(any(row["key"].startswith("weather_forecast_slot:") for row in db.rows["app_settings"]))
        self.assertEqual(report["stages"]["forecast"]["reason"], "WEATHER_RESEARCH_HOUR_EXPIRED")


if __name__ == "__main__":
    unittest.main()
