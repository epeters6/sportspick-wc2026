"""Forward evidence cannot confuse snapshots, theoretical profits, or venue accounts."""
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace
import json
import unittest

from backend.trading.weather_forward_report import _bootstrap_roi, build_forward_report


MANIFEST = {"id": "forward-test", "config_hash": "frozen-test-hash", "config": {
    "prediction_source": "weather_forward_v1", "min_forward_events": 250, "min_forward_days": 45,
}}


class Query:
    def __init__(self, database, table):
        self.database, self.table_name, self.start, self.end = database, table, 0, None
        self.sort_key, self.cursor = None, None
    def select(self, *_):
        return self
    def eq(self, key, value):
        self.database.filters.append((self.table_name, key, value))
        return self
    def order(self, key):
        self.sort_key = key
        return self
    def limit(self, count):
        self.end = count - 1
        return self
    def gt(self, key, value):
        self.cursor = (key, value)
        self.database.cursors.append((self.table_name, key, value))
        return self
    def range(self, start, end):
        self.start, self.end = start, end
        self.database.ranges.append((self.table_name, start, end))
        return self
    def execute(self):
        if self.table_name in self.database.fail:
            raise RuntimeError("test database read failure")
        rows = self.database.rows.get(self.table_name, [])
        if self.sort_key:
            rows = sorted(rows, key=lambda row: row.get(self.sort_key, ""))
        if self.cursor and not self.database.ignore_cursor:
            key, value = self.cursor
            rows = [row for row in rows if row[key] > value]
        rows = rows[self.start:None if self.end is None else self.end + 1]
        return SimpleNamespace(data=rows[:self.database.server_cap])


class Database:
    """Intentionally ignores filters so the report's defensive scope checks are tested."""
    def __init__(self, predictions=None, bets=None, clv=None, fail=(), server_cap=None, ignore_cursor=False):
        self.rows = {"model_predictions": predictions or [], "autobets": bets or [], "clv_obligations": clv or []}
        self.filters, self.ranges, self.fail = [], [], set(fail)
        self.cursors, self.server_cap, self.ignore_cursor = [], server_cap, ignore_cursor
    def table(self, name):
        return Query(self, name)


def fixture(index=0, venue="kalshi", run="run-1", *, won=True):
    day = (date(2026, 1, 1) + timedelta(days=index // 5)).isoformat()
    station = f"K{index % 5}"
    event = f"weather:{venue}:{station}:{day}:high"
    prefix = f"{venue}-{index}"
    meta = {"experiment_id": MANIFEST["id"], "config_hash": MANIFEST["config_hash"],
            "platform": venue, "station": station, "target_date": day, "metric": "high",
            "run_id": run, "snapshot_kind": "pre_execution_forecast", "label_source": "venue_official",
            "official_venue": venue}
    rows = []
    for outcome in range(2):
        market = f"{prefix}-bucket-{outcome}"
        correct = outcome == (0 if won else 1)
        rows.append({"id": f"snapshot-{prefix}-{run}-{outcome}", "source": "weather_forward_v1",
            "event_key": event, "outcome": market, "prob": [0.7, 0.3][outcome], "market_price": 0.5,
            "created_at": f"{day}T10:00:00Z", "resolved_at": f"{day}T23:00:00Z", "is_correct": correct,
            "metadata": {**meta, "official_market_id": market, "official_result": "yes" if correct else "no",
                         "bucket_low_f": None if outcome == 0 else 75.5, "bucket_high_f": 75.5 if outcome == 0 else None}})
    bet = {"id": f"bet-{prefix}", "venue": venue, "mode": "paper", "sport": "weather", "market_id": rows[0]["outcome"],
        "outcome_name": "yes", "stake": 0.5, "shares": 1, "market_price": 0.48, "status": "won" if won else "lost",
        "pnl": 0.5 if won else -0.5, "created_at": f"{day}T10:00:00Z", "resolved_at": f"{day}T23:00:00Z",
        "metadata": {**meta, "q_exec": 0.5, "candidate_id": f"candidate-{prefix}", "decision_snapshot_id": rows[0]["id"],
            "settlement": {"version": "weather_venue_official_v3", "source": f"{venue}_official",
                           "official_result": "yes" if won else "no", "market_id": rows[0]["outcome"]}}}
    clv = {"candidate_id": f"candidate-{prefix}", "platform": venue, "market_id": bet["market_id"], "side": "YES",
        "entry_effective_cost": 0.5, "entry_ts": f"{day}T10:00:00Z", "due_close": f"{day}T22:00:00Z",
        "status_close": "observed", "obs_close_ts": f"{day}T22:00:00Z", "obs_close_price": 0.6,
        "metadata": {**meta, "autobet_id": bet["id"]}}
    return rows, bet, clv


def profitable_sample(count=250):
    predictions, bets, clv = [], [], []
    for index in range(count):
        vector, bet, close = fixture(index)
        predictions.extend(vector)
        bets.append(bet)
        clv.append(close)
    return predictions, bets, clv


class TestWeatherForwardReport(unittest.TestCase):

    def test_forward_source_is_selected_from_the_manifest_and_isolates_arms(self):
        predictions, bet, clv = fixture()
        manifest = deepcopy(MANIFEST)
        manifest["id"] = "forward-challenger"
        manifest["config"]["prediction_source"] = "weather_forward_v2_edge03"
        challenger_predictions, challenger_bet, challenger_clv = deepcopy((predictions, bet, clv))
        for row in challenger_predictions:
            row["source"] = manifest["config"]["prediction_source"]
            row["metadata"]["experiment_id"] = manifest["id"]
        challenger_bet["metadata"]["experiment_id"] = manifest["id"]
        challenger_clv["metadata"]["experiment_id"] = manifest["id"]
        database = Database(predictions + challenger_predictions,
                            [bet, challenger_bet], [clv, challenger_clv])
        report = build_forward_report(database, manifest)
        self.assertEqual(report["forecast"]["scoped_rows"], 2)
        self.assertEqual(report["venues"]["kalshi"]["verified_settled_bets"], 1)
        self.assertEqual(report["venues"]["kalshi"]["closing_clv_complete_events"], 1)
        self.assertEqual(report["prediction_source"], "weather_forward_v2_edge03")
        self.assertIn(("model_predictions", "source", "weather_forward_v2_edge03"), database.filters)
        self.assertFalse(report["live_ready"])

    def test_non_forward_source_is_not_a_valid_forward_manifest(self):
        for source in (None, "", "weather_calibrated_v2", "mlb_quant_all_games_v2",
                       "weather_forward_", ["weather_forward_v2"]):
            with self.subTest(source=source):
                manifest = deepcopy(MANIFEST)
                manifest["config"]["prediction_source"] = source
                with self.assertRaisesRegex(ValueError, "INVALID_WEATHER_FORWARD_MANIFEST"):
                    build_forward_report(Database(), manifest)

    def test_actual_fill_obligation_id_receives_closing_coverage(self):
        predictions, bet, clv = fixture()
        clv["candidate_id"] = f"weather-fill:{bet['id']}"
        clv["metadata"]["signal_candidate_id"] = bet["metadata"]["candidate_id"]
        report = build_forward_report(Database(predictions, [bet], [clv]), MANIFEST)
        venue = report["venues"]["kalshi"]
        self.assertEqual(venue["closing_clv_observed_bets"], 1)
        self.assertEqual(venue["closing_clv_event_coverage"], 1)
        self.assertAlmostEqual(venue["mean_net_closing_clv"], 0.1)
        self.assertEqual(venue["quarantined"], 0)

    def test_fill_clv_requires_matching_trade_provenance(self):
        for field in ("signal_candidate_id", "autobet_id", "config_hash", "missing_signal"):
            with self.subTest(field=field):
                predictions, bet, clv = fixture()
                clv["candidate_id"] = f"weather-fill:{bet['id']}"
                clv["metadata"]["signal_candidate_id"] = bet["metadata"]["candidate_id"]
                if field == "missing_signal":
                    del clv["metadata"]["signal_candidate_id"]
                else:
                    clv["metadata"][field] = "another-record"
                report = build_forward_report(Database(predictions, [bet], [clv]), MANIFEST)
                venue = report["venues"]["kalshi"]
                self.assertEqual(venue["closing_clv_observed_bets"], 0)
                self.assertEqual(venue["quarantine_reasons"]["INVALID_TRADED_CLOSING_CLV"], 1)

    def test_other_experiment_fill_clv_cannot_cover_current_trade(self):
        predictions, bet, clv = fixture()
        clv["candidate_id"] = f"weather-fill:{bet['id']}"
        clv["metadata"]["signal_candidate_id"] = bet["metadata"]["candidate_id"]
        clv["metadata"]["experiment_id"] = "another-experiment"
        report = build_forward_report(Database(predictions, [bet], [clv]), MANIFEST)
        self.assertEqual(report["venues"]["kalshi"]["closing_clv_observed_bets"], 0)
        self.assertEqual(report["venues"]["kalshi"]["closing_clv_event_coverage"], 0)

    def test_ambiguous_fill_and_signal_obligations_cannot_both_count(self):
        predictions, bet, legacy_clv = fixture()
        fill_clv = deepcopy(legacy_clv)
        fill_clv["candidate_id"] = f"weather-fill:{bet['id']}"
        fill_clv["metadata"]["signal_candidate_id"] = bet["metadata"]["candidate_id"]
        report = build_forward_report(Database(predictions, [bet], [legacy_clv, fill_clv]), MANIFEST)
        venue = report["venues"]["kalshi"]
        self.assertEqual(venue["closing_clv_observed_bets"], 0)
        self.assertEqual(venue["quarantine_reasons"]["AMBIGUOUS_TRADED_CLOSING_CLV"], 1)

    def test_exploratory_calibration_mode_is_visible_and_never_enables_live(self):
        manifest = deepcopy(MANIFEST)
        manifest["config"].update({"execution_calibration_mode": "observe_only",
                                   "research_label": "Forecast-only paper challenger",
                                   "exploratory": True})
        report = build_forward_report(Database(), manifest)
        self.assertEqual(report["execution_calibration_mode"], "observe_only")
        self.assertEqual(report["research_label"], "Forecast-only paper challenger")
        self.assertTrue(report["exploratory"])
        self.assertFalse(report["historical_execution_filter_required"])
        self.assertFalse(report["live_ready"])
        self.assertIn("does not enforce", report["limitations"][-1])

    def test_live_or_unknown_calibration_manifest_is_rejected(self):
        for patch in ({"mode": "live"}, {"execution_calibration_mode": "disabled"}):
            manifest = deepcopy(MANIFEST)
            manifest["config"].update(patch)
            with self.assertRaisesRegex(ValueError, "INVALID_WEATHER_FORWARD_MANIFEST"):
                build_forward_report(Database(), manifest)

    def test_malformed_signal_identity_is_quarantined_without_failing_report(self):
        predictions, bet, clv = fixture()
        bet["metadata"]["candidate_id"] = ["invalid", "identity"]
        report = build_forward_report(Database(predictions, [bet], [clv]), MANIFEST)
        self.assertEqual(report["venues"]["kalshi"]["closing_clv_observed_bets"], 0)
        self.assertEqual(report["venues"]["kalshi"]["quarantine_reasons"]["INVALID_TRADED_CLOSING_CLV"], 1)

    def test_empty_data_is_collecting_and_never_live_ready(self):
        report = build_forward_report(Database(), MANIFEST)
        self.assertFalse(report["live_ready"])
        self.assertFalse(report["research_evidence_ready"])
        self.assertEqual(report["forecast"]["snapshots"], 0)
        for venue in report["venues"].values():
            self.assertIsNone(venue["net_roi"])
            self.assertEqual(venue["verified_realized_pnl"], 0)
            self.assertFalse(venue["research_evidence_ready"])

    def test_wrong_source_experiment_and_live_rows_cannot_enter_paper_report(self):
        predictions, bet, clv = fixture()
        predictions[0]["source"] = "weather_v1"
        predictions[1]["metadata"]["experiment_id"] = "old-experiment"
        bet["mode"] = "live"
        report = build_forward_report(Database(predictions, [bet], [clv]), MANIFEST)
        self.assertEqual(report["forecast"]["scoped_rows"], 0)
        self.assertEqual(report["venues"]["kalshi"]["verified_settled_bets"], 0)

    def test_repeated_snapshots_and_cross_venue_events_are_not_independent_trials(self):
        first, bet, _ = fixture()
        second, _, _ = fixture(run="run-2")
        other, _, _ = fixture(venue="polymarket")
        report = build_forward_report(Database(first + deepcopy(first) + second + other), MANIFEST, bets=[])
        forecast = report["forecast"]
        self.assertEqual(forecast["official_valid_snapshots"], 3)
        self.assertEqual(forecast["independent_events"], 1)
        self.assertEqual(forecast["venues"]["kalshi"]["independent_events"], 1)
        self.assertAlmostEqual(forecast["venues"]["kalshi"]["model_brier"], 0.09)
        self.assertAlmostEqual(forecast["venues"]["kalshi"]["market_brier"], 0.25)

    def test_official_two_winner_vector_is_quarantined(self):
        predictions, _, _ = fixture()
        predictions[1]["is_correct"] = True
        predictions[1]["metadata"]["official_result"] = "yes"
        report = build_forward_report(Database(predictions), MANIFEST, bets=[])
        self.assertEqual(report["forecast"]["official_valid_snapshots"], 0)
        self.assertEqual(report["forecast"]["quarantine_reasons"]["OFFICIAL_VECTOR_WINNER_COUNT"], 1)

    def test_station_label_or_incomplete_ladder_cannot_be_official_evidence(self):
        for change in ("station_label", "ladder_gap"):
            predictions, _, _ = fixture()
            if change == "station_label":
                predictions[0]["metadata"]["label_source"] = "station_metar"
            else:
                predictions[1]["metadata"]["bucket_low_f"] = 77.5
            report = build_forward_report(Database(predictions), MANIFEST, bets=[])
            self.assertEqual(report["forecast"]["official_valid_snapshots"], 0)
            self.assertEqual(report["forecast"]["quarantined"], 1)

    def test_only_verified_effective_cost_pnl_counts_once_and_by_venue(self):
        predictions, bet, clv = fixture()
        other_vector, other_bet, other_clv = fixture(venue="polymarket", won=False)
        corrupt = deepcopy(bet)
        corrupt["id"], corrupt["pnl"] = "wrong-pnl", 1000
        theoretical = deepcopy(clv)
        theoretical["candidate_id"] = "untraded-candidate"
        theoretical["pnl"] = 50000
        report = build_forward_report(Database(predictions + other_vector, [bet, deepcopy(bet), corrupt, other_bet], [clv, other_clv, theoretical]), MANIFEST)
        self.assertEqual(report["venues"]["kalshi"]["verified_realized_pnl"], 0.5)
        self.assertEqual(report["venues"]["polymarket"]["verified_realized_pnl"], -0.5)
        self.assertEqual(report["venues"]["kalshi"]["quarantined"], 1)

    def test_sufficient_profitable_paper_evidence_never_promotes_live(self):
        predictions, bets, clv = profitable_sample()
        database = Database(predictions, bets, clv)
        report = build_forward_report(database, MANIFEST)
        kalshi = report["venues"]["kalshi"]
        self.assertEqual(kalshi["independent_traded_events"], 250)
        self.assertEqual(kalshi["distinct_traded_dates"], 50)
        self.assertTrue(kalshi["research_evidence_ready"])
        self.assertFalse(kalshi["live_ready"])
        self.assertFalse(report["live_ready"])
        self.assertFalse(report["research_evidence_ready"])  # The other venue has no evidence.
        self.assertEqual(report["research_evidence_ready_venues"], ["kalshi"])
        self.assertTrue(any(table == "model_predictions" and key == "id" for table, key, _ in database.cursors))
        self.assertIn(("model_predictions", "source", "weather_forward_v1"), database.filters)

    def test_closing_clv_coverage_is_required_even_with_profitable_pnl(self):
        predictions, bets, clv = profitable_sample()
        report = build_forward_report(Database(predictions, bets, clv[:100]), MANIFEST)
        kalshi = report["venues"]["kalshi"]
        self.assertEqual(kalshi["closing_clv_event_coverage"], 0.4)
        self.assertFalse(kalshi["research_evidence_ready"])
        self.assertIn("closing_clv_event_coverage", kalshi["blocked_reasons"])

    def test_single_lucky_day_does_not_pass_day_cluster_interval(self):
        predictions, bets, clv = [], [], []
        for index in range(250):
            vector, bet, close = fixture(index, won=index < 5)
            if index < 5:
                bet["shares"], bet["stake"], bet["pnl"] = 100, 50, 50
            predictions.extend(vector)
            bets.append(bet)
            clv.append(close)
        report = build_forward_report(Database(predictions, bets, clv), MANIFEST)
        kalshi = report["venues"]["kalshi"]
        self.assertGreater(kalshi["net_roi"], 0)
        self.assertLess(kalshi["net_roi_95pct_day_cluster_interval"][0], 0)
        self.assertFalse(kalshi["research_evidence_ready"])

    def test_db_failure_blocks_readiness_instead_of_silently_passing(self):
        predictions, bets, clv = profitable_sample()
        report = build_forward_report(Database(predictions, bets, clv, fail=["clv_obligations"]), MANIFEST)
        self.assertTrue(report["read_errors"])
        self.assertFalse(report["venues"]["kalshi"]["criteria"]["reads_complete"])

    def test_server_cap_smaller_than_requested_page_preserves_all_evidence(self):
        predictions, bets, clv = profitable_sample(12)
        database = Database(predictions, bets, clv, server_cap=3)
        report = build_forward_report(database, MANIFEST)
        self.assertFalse(report["read_errors"])
        self.assertEqual(report["forecast"]["scoped_rows"], 24)
        self.assertEqual(report["forecast"]["official_valid_snapshots"], 12)
        self.assertEqual(report["venues"]["kalshi"]["verified_settled_bets"], 12)
        self.assertEqual(report["venues"]["kalshi"]["closing_clv_complete_events"], 12)
        self.assertEqual({table for table, _, _ in database.cursors}, {"model_predictions", "autobets", "clv_obligations"})

    def test_nonadvancing_cursor_fails_closed_instead_of_looping(self):
        predictions, bets, clv = profitable_sample(12)
        report = build_forward_report(Database(predictions, bets, clv, server_cap=3, ignore_cursor=True), MANIFEST)
        self.assertEqual(len(report["read_errors"]), 3)
        self.assertFalse(report["research_evidence_ready"])

    def test_bootstrap_rejects_nonfinite_values(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertIsNone(_bootstrap_roi([
                    {"event": ("KNYC", "2026-01-01", "high"), "stake": 1, "pnl": value},
                    {"event": ("KNYC", "2026-01-02", "high"), "stake": 1, "pnl": 1},
                ]))

    def test_overflowing_account_totals_are_quarantined_and_json_finite(self):
        predictions, bets, clv = profitable_sample(4)
        for bet in bets:
            bet.update({"shares": 1.7e308, "stake": 8.5e307, "pnl": 8.5e307})
        report = build_forward_report(Database(predictions, bets, clv), MANIFEST)
        kalshi = report["venues"]["kalshi"]
        self.assertIsNone(kalshi["verified_stake"])
        self.assertIsNone(kalshi["verified_realized_pnl"])
        self.assertIn("NON_FINITE_ACCOUNT_AGGREGATE", kalshi["quarantine_reasons"])
        self.assertFalse(kalshi["research_evidence_ready"])
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
