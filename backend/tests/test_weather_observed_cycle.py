"""The observer must not alter frozen decisions or hide failed cycles."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_weather_observed_cycle import run_observed_suite, suite_exit_code, render_discovery_markdown


NOW = datetime(2026, 9, 13, 12, 7, tzinfo=timezone.utc)


class WeatherObservedCycleTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, broken_component=None, default_clock=False, discovery_status="ok"):
        shared = {"forecast_hour": "20260913T12"}
        config = {"id": "one", "mode": "paper"}
        db = object()
        report = {
            "status": "degraded", "mode": "paper", "run_id": "suite",
            "timestamp": NOW.isoformat(), "stages": {"accounting": {"status": "ok"}},
            "experiments": {"one": {"venues": {"kalshi": {"realized_pnl": -4}}}},
        }
        saved_report = deepcopy(report)
        calls = []

        async def core_cycle(**kwargs):
            calls.append(kwargs)
            self.assertIs(kwargs["db"], db)
            self.assertIs(kwargs["experiment_config"], config)
            self.assertIs(kwargs["shared_inputs"], shared)
            self.assertEqual(shared, {"forecast_hour": "20260913T12"})
            shared["market_capture"] = {"markets": [{"ticker": "public-fixture"}]}
            return report

        async def core_suite(**kwargs):
            self.assertIs(kwargs["db"], db)
            self.assertEqual(kwargs["now"], None if default_clock else NOW)
            self.assertIs(kwargs["configs"][0], config)
            result = await kwargs["cycle_runner"](
                db=db, experiment_config=config, shared_inputs=shared, now=NOW,
                include_global_stages=True, persist_latest=False,
                report_path=Path(kwargs["report_path"]).parent / "one.json",
            )
            Path(kwargs["report_path"]).write_text(json.dumps(result), encoding="utf-8")
            return result

        def discovery(actual_shared, actual_configs, *, now):
            self.assertEqual(len(calls), 1)
            self.assertIs(actual_shared, shared)
            self.assertIs(actual_configs[0], config)
            self.assertEqual(now, NOW)
            return {"status": discovery_status, "capture_id": "captured-public-data"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            with patch("backend.trading.weather_opportunity_diagnostics.build_discovery_diagnostics",
                       side_effect=RuntimeError("secret-must-not-appear") if broken_component == "discovery" else discovery), \
                 patch("backend.trading.weather_performance_scorecard.build_performance_scorecard",
                       side_effect=RuntimeError("secret-must-not-appear") if broken_component == "performance" else None,
                       return_value={"arms": [{"id": "one"}]}), \
                 patch("backend.trading.weather_performance_scorecard.render_scorecard_markdown",
                       return_value="Paper only; insufficient evidence.\n"):
                actual, evidence = await run_observed_suite(
                    report_path=path, db=db, configs=[config], now=None if default_clock else NOW,
                    suite_runner=core_suite, cycle_runner=core_cycle,
                )
            self.assertIs(actual, report)
            self.assertEqual(report, saved_report)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), saved_report)
            self.assertEqual(json.loads((Path(directory) / "opportunities.json").read_text(encoding="utf-8")), evidence)
            self.assertTrue((Path(directory) / "performance.md").is_file())
            self.assertNotIn("secret-must-not-appear", json.dumps(evidence))
        return evidence

    async def test_observer_runs_after_core_with_identical_arguments_and_separate_artifacts(self):
        evidence = await self.exercise()
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["discovery"]["capture_id"], "captured-public-data")
        self.assertFalse(evidence["live_ready"])

    async def test_default_clock_is_owned_by_core_after_database_initialization(self):
        evidence = await self.exercise(default_clock=True)
        self.assertEqual(evidence["status"], "ok")

    async def test_discovery_failure_does_not_hide_completed_accounting_or_scorecard(self):
        evidence = await self.exercise(broken_component="discovery")
        self.assertEqual(evidence["status"], "degraded")
        self.assertEqual(evidence["components"]["discovery"]["error"], "RuntimeError")
        self.assertEqual(evidence["components"]["performance"]["status"], "ok")

    async def test_scorecard_failure_preserves_discovery_and_authoritative_report(self):
        evidence = await self.exercise(broken_component="performance")
        self.assertEqual(evidence["status"], "degraded")
        self.assertEqual(evidence["components"]["performance"]["error"], "RuntimeError")
        self.assertEqual(evidence["components"]["discovery"]["status"], "ok")

    async def test_returned_diagnostic_failure_is_visible_but_absent_capture_is_not_failure(self):
        for status in ("invalid_capture", "diagnostic_error", "empty_capture", "incomplete_capture"):
            evidence = await self.exercise(discovery_status=status)
            self.assertEqual(evidence["status"], "degraded")
            self.assertEqual(evidence["components"]["discovery"]["status"], "failed")
        for status in ("not_captured", "issues_found"):
            evidence = await self.exercise(discovery_status=status)
            self.assertEqual(evidence["status"], "ok")

    def test_no_capture_summary_does_not_invent_zero_opportunities(self):
        markdown = render_discovery_markdown({"status": "not_captured"})
        self.assertIn("does not mean there are zero opportunities", markdown)
        self.assertNotIn("| 0 |", markdown)

    def test_secondary_arm_failure_retains_original_nonzero_exit_contract(self):
        self.assertEqual(suite_exit_code({"status": "degraded", "stages": {
            "research_arms": {"status": "failed"}}}), 1)
        self.assertEqual(suite_exit_code({"status": "failed", "stages": {}}), 1)
        self.assertEqual(suite_exit_code({"status": "degraded", "stages": {
            "forecast": {"status": "degraded"}}}), 0)


if __name__ == "__main__":
    unittest.main()
