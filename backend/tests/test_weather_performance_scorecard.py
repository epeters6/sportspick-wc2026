"""Report-only economics must preserve evidence gaps and separate paper accounts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from backend.trading import weather_performance_scorecard as score

NOW = datetime(2026, 9, 13, 15, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz or timezone.utc)


def account(pnl=0, settled=0, opened=0, reserved=0):
    return {"initial_bankroll": 500, "realized_pnl": pnl, "equity": 500 + pnl,
            "available_cash": 500 + pnl - reserved, "reserved": reserved,
            "open": opened, "settled": settled, "quarantined": 0, "blocked_reasons": []}


def evaluation(pnl=0, settled=0, opened=0):
    criteria = {key: False for key in score.CRITERIA}
    criteria.update(reads_complete=True, no_quarantined_evidence=True)
    return {"criteria": criteria, "verified_settled_bets": settled, "verified_realized_pnl": pnl,
            "verified_stake": settled * 2, "independent_traded_events": settled,
            "distinct_traded_dates": 1 if settled else 0, "open_bets": opened,
            "quarantined": 0, "mean_net_closing_clv": .04 if settled else None,
            "closing_clv_complete_events": settled, "closing_clv_observed_bets": settled,
            "closing_clv_event_coverage": 1 if settled else 0}


def arm(identity="baseline", *, pnl=0, settled=0):
    return {"mode": "paper", "status": "healthy", "completed_at": NOW.isoformat(),
            "experiment": {"id": identity, "config": {"research_label": identity,
                "seed_per_venue": 500, "max_event_risk_fraction": .005,
                "max_open_risk_fraction": .05, "min_net_edge": .05,
                "min_forward_events": 250, "min_forward_days": 45}},
            "stages": {"accounting": {"status": "ok"}, "forward_evaluation": {"status": "ok"},
                       "forecast": {"status": "ok", "result": {"events": 10, "bets_placed": 0,
                            "kelly_reject": 8, "scope_skipped": 6,
                            "rejection_reasons": {"OUTSIDE_WEATHER_DECISION_WINDOW": 6,
                                                  "NO_BUCKET_MEETS_MIN_NET_EDGE": 8}}}},
            "venues": {"kalshi": account(pnl, settled), "polymarket": account()},
            "forward_evaluation": {"experiment_id": identity, "generated_at": NOW.isoformat(),
                "read_errors": [], "venues": {"kalshi": evaluation(pnl, settled), "polymarket": evaluation()}}}


class PerformanceScorecardTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(score, "datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)

    def build(self, report):
        before = deepcopy(report)
        result = score.build_performance_scorecard(report)
        self.assertEqual(report, before)
        json.dumps(result, allow_nan=False)
        return result

    def test_one_winner_is_descriptive_not_profitability_or_income_evidence(self):
        result = self.build(arm(pnl=17.01, settled=1))
        venue = result["experiments"]["baseline"]["venues"]["kalshi"]
        self.assertEqual(venue["verified_evaluation"]["net_per_verified_settled_trade"], 17.01)
        self.assertEqual(venue["evidence_status"], "insufficient")
        self.assertEqual(venue["progress"]["verified_traded_events"], {"observed": 1, "required": 250, "remaining": 249})
        self.assertEqual(venue["progress"]["verified_traded_dates"]["remaining"], 44)
        self.assertFalse(result["live_ready"])
        for name in ("annualized_roi", "projected_income", "daily_income", "combined_balance"):
            self.assertNotIn(name, json.dumps(result))

    def test_zero_trades_have_undefined_economics_not_a_zero_return_estimate(self):
        result = self.build(arm())
        verified = result["experiments"]["baseline"]["venues"]["kalshi"]["verified_evaluation"]
        self.assertEqual(verified["verified_settled_bets"], 0)
        self.assertEqual(verified["verified_realized_pnl"], 0)
        for name in ("net_per_verified_settled_trade", "net_roi", "mean_net_closing_clv", "closing_clv_event_coverage"):
            self.assertIsNone(verified[name])
        self.assertEqual(result["evidence_status"], "insufficient")

    def test_failed_reads_cannot_be_presented_as_zero_trade_evidence(self):
        source = arm(pnl=12, settled=2)
        source["forward_evaluation"]["read_errors"] = ["BET_READ_FAILED:https://private.test/?secret=do-not-print"]
        source["forward_evaluation"]["venues"]["kalshi"] = evaluation()
        result = self.build(source)
        venue = result["experiments"]["baseline"]["venues"]["kalshi"]
        self.assertEqual(venue["accounting"]["booked_pnl"], 12)
        self.assertIsNone(venue["verified_evaluation"]["verified_settled_bets"])
        self.assertIsNone(venue["verified_evaluation"]["verified_realized_pnl"])
        self.assertEqual(venue["evidence_status"], "incomplete")
        self.assertNotIn("do-not-print", json.dumps(result) + score.render_scorecard_markdown(result))
        self.assertIn("Missing evidence is not a zero-trade result", score.render_scorecard_markdown(result))

    def test_failed_accounting_does_not_invent_cash_from_the_configured_seed(self):
        source = arm(pnl=4, settled=1)
        source["stages"]["accounting"] = {"status": "failed", "error": "password=do-not-print"}
        result = self.build(source)
        venue = result["experiments"]["baseline"]["venues"]["kalshi"]
        self.assertIsNone(venue["accounting"]["available"])
        self.assertIsNone(venue["constraints"]["available_event_budget_before_market_checks"])
        self.assertEqual(venue["verified_evaluation"]["verified_realized_pnl"], 4)
        self.assertNotIn("do-not-print", json.dumps(result))

    def test_arms_and_venues_never_pool_or_count_the_primary_twice(self):
        first, second = arm("first", pnl=10, settled=1), arm("second", pnl=-7, settled=1)
        first["venues"]["polymarket"] = account(-2, 1)
        first["forward_evaluation"]["venues"]["polymarket"] = evaluation(-2, 1)
        source = {**first, "experiments": {"first": first, "second": second}, "active_experiment_ids": ["first", "second"]}
        result = self.build(source)
        self.assertEqual(len(result["experiments"]), 2)
        self.assertEqual(result["experiments"]["first"]["venues"]["kalshi"]["accounting"]["booked_pnl"], 10)
        self.assertEqual(result["experiments"]["first"]["venues"]["polymarket"]["accounting"]["booked_pnl"], -2)
        self.assertEqual(result["experiments"]["second"]["venues"]["kalshi"]["accounting"]["booked_pnl"], -7)
        self.assertNotIn("venues", result)

    def test_overlapping_rejection_counts_stay_exact_and_arm_wide(self):
        result = self.build(arm())
        experiment = result["experiments"]["baseline"]
        cycle = experiment["latest_cycle"]
        self.assertEqual(cycle["top_reasons"], [
            {"reason": "NO_BUCKET_MEETS_MIN_NET_EDGE", "count": 8},
            {"reason": "OUTSIDE_WEATHER_DECISION_WINDOW", "count": 6}])
        self.assertEqual(cycle["counters"]["events"], 10)
        self.assertEqual(cycle["counters"]["kelly_reject"], 8)
        self.assertEqual(cycle["scope"], "latest_forecast_cycle_all_venues")
        self.assertNotIn("total_rejections", cycle)
        self.assertNotIn("rejections", experiment["venues"]["kalshi"])

    def test_accounting_evaluation_differences_are_visible_not_reconciled(self):
        source = arm(pnl=10, settled=2)
        source["forward_evaluation"]["venues"]["kalshi"] = evaluation(7, 1)
        venue = self.build(source)["experiments"]["baseline"]["venues"]["kalshi"]
        self.assertIn("SETTLED_COUNT_DIFFERS", venue["discrepancies"])
        self.assertIn("BOOKED_VS_VERIFIED_PNL_DIFFERS", venue["discrepancies"])
        self.assertEqual(venue["accounting"]["booked_pnl"], 10)
        self.assertEqual(venue["verified_evaluation"]["verified_realized_pnl"], 7)
        self.assertEqual(venue["evidence_status"], "incomplete")

    def test_small_selection_blockers_remain_visible_behind_large_scope_counts(self):
        source = arm()
        source["stages"]["forecast"]["result"]["rejection_reasons"] = {
            "UNSUPPORTED_SETTLEMENT_STATION": 264, "UNVERIFIED_OBSERVATION_DAY": 96,
            "MISSING_EXPLICIT_SETTLEMENT_STATION": 35, "OUTSIDE_WEATHER_DECISION_WINDOW": 20,
            "INVALID_WEATHER_BUCKET_PARTITION": 4, "NO_BUCKET_MEETS_MIN_NET_EDGE": 2,
            "WEATHER_EXECUTION_COHORT_NOT_PROFITABLE": 1,
        }
        markdown = score.render_scorecard_markdown(self.build(source))
        self.assertIn("No contract meets minimum net edge=2", markdown)
        self.assertIn("Historical comparison group failed profitability filter=1", markdown)

    def test_missing_or_wrong_identity_masks_inconsistent_accounts(self):
        for manifest_id in (None, "another-arm"):
            source = arm(pnl=10, settled=1)
            source["experiment"]["id"] = manifest_id
            result = self.build({"completed_at": NOW.isoformat(), "experiments": {"baseline": source}})
            experiment = result["experiments"]["baseline"]
            self.assertTrue(experiment["identity_mismatch"])
            self.assertIsNone(experiment["venues"]["kalshi"]["accounting"]["booked_pnl"])
            self.assertIsNone(experiment["venues"]["kalshi"]["verified_evaluation"]["verified_realized_pnl"])

    def test_wrong_evaluation_identity_cannot_replace_valid_accounting(self):
        source = arm(pnl=10, settled=1)
        source["forward_evaluation"]["experiment_id"] = "other-arm"
        venue = self.build(source)["experiments"]["baseline"]["venues"]["kalshi"]
        self.assertEqual(venue["accounting"]["booked_pnl"], 10)
        self.assertIsNone(venue["verified_evaluation"]["verified_realized_pnl"])

    def test_missing_arm_and_stale_or_future_report_are_incomplete(self):
        for value, state in (((NOW - timedelta(hours=3)).isoformat(), "stale"),
                             ((NOW + timedelta(hours=1)).isoformat(), "future_timestamp"),
                             (None, "missing_or_invalid")):
            source = arm()
            source["completed_at"] = value
            result = self.build(source)
            self.assertEqual(result["freshness"]["state"], state)
            self.assertEqual(result["evidence_status"], "incomplete")
        result = self.build({"experiments": {"baseline": arm()}, "active_experiment_ids": ["baseline", "missing"]})
        self.assertEqual(result["experiments"]["missing"]["evidence_status"], "incomplete")

    def test_nonfinite_values_and_bad_counts_never_enter_json(self):
        source = arm()
        source["venues"]["kalshi"]["available_cash"] = float("inf")
        source["forward_evaluation"]["venues"]["kalshi"]["verified_settled_bets"] = True
        source["stages"]["forecast"]["result"]["rejection_reasons"]["BAD_COUNT"] = -1
        result = self.build(source)
        experiment = result["experiments"]["baseline"]
        self.assertIsNone(experiment["venues"]["kalshi"]["accounting"]["available"])
        self.assertIsNone(experiment["venues"]["kalshi"]["verified_evaluation"]["verified_settled_bets"])
        self.assertEqual(experiment["latest_cycle"]["invalid_count_entries"], 1)

    def test_configured_progress_floors_and_reported_gate_conflicts(self):
        source = arm(settled=1)
        source["experiment"]["config"].update(min_forward_events=1, min_forward_days=60)
        source["forward_evaluation"]["venues"]["kalshi"]["criteria"] = {key: True for key in score.CRITERIA}
        venue = self.build(source)["experiments"]["baseline"]["venues"]["kalshi"]
        self.assertEqual(venue["progress"]["verified_traded_events"]["required"], 250)
        self.assertEqual(venue["progress"]["verified_traded_dates"]["required"], 60)
        self.assertEqual(venue["evidence_status"], "incomplete")
        self.assertIn("REPORTED_CRITERION_DISAGREES_WITH_COUNTS", venue["discrepancies"])

    def test_risk_capacity_uses_separate_current_cash_and_no_profit_projection(self):
        source = arm()
        source["venues"]["kalshi"] = account(0, 0, opened=10, reserved=24)
        source["forward_evaluation"]["venues"]["kalshi"]["open_bets"] = 10
        venue = self.build(source)["experiments"]["baseline"]["venues"]["kalshi"]
        self.assertEqual(venue["constraints"]["current_event_cost_cap"], 2.5)
        self.assertEqual(venue["constraints"]["available_event_budget_before_market_checks"], 1)
        self.assertEqual(venue["constraints"]["minimum_net_edge_per_share"], .05)

    def test_markdown_escapes_labels_and_stays_compact_for_three_arms(self):
        source = arm()
        source["experiment"]["config"]["research_label"] = '[click](javascript:alert(1)) <img src=x>\n| injected | @everyone'
        result = self.build(source)
        markdown = score.render_scorecard_markdown(result)
        self.assertNotIn("[click](javascript:", markdown)
        self.assertNotIn("<img", markdown)
        self.assertNotIn("\n| injected", markdown)
        self.assertNotIn("@everyone", markdown)
        suite = {"completed_at": NOW.isoformat(), "experiments": {key: arm(key) for key in ("a", "b", "c")}}
        markdown = score.render_scorecard_markdown(self.build(suite))
        self.assertLess(len(markdown), 11000)
        self.assertLess(len(markdown.splitlines()), 100)
        self.assertIn("No contract meets minimum net edge=8", markdown)
        table_rows = [line for line in markdown.splitlines() if line.startswith("|")]
        self.assertEqual(len(table_rows), 24)
        self.assertTrue(all(len(line.split("|")) - 2 == 8 for line in table_rows))


if __name__ == "__main__":
    unittest.main()
