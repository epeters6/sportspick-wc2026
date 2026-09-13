"""Captured-input diagnostics must explain exclusions without affecting strategy."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from backend.trading import weather_opportunity_diagnostics as diagnostics
from backend.trading.weather_experiment import IMPLEMENTATION_FILES, implementation_hash
from pavlov.pipeline.settlement_resolver import normalize_market


NOW = datetime(2026, 9, 13, 12, 30, tzinfo=timezone.utc)
CONFIG = {"id": "weather_test_v2", "venues": ["kalshi", "polymarket"],
          "stations": ["KNYC"], "metrics": ["high"], "max_lead_days": 1,
          "decision_hours_local": [8, 11, 14]}


def markets(venue="polymarket", target="2026-09-13"):
    day = datetime.fromisoformat(target)
    close = (day + timedelta(days=1, hours=5)).replace(tzinfo=timezone.utc)
    rows = []
    for index, (kind, title, suffix) in enumerate((
        ("less", "New York high temperature <80 F", "lt80f"),
        ("greater", "New York high temperature 80 or above", "gte80f"),
    )):
        ticker = (f"tc-temp-nychigh-{target}-{suffix}" if venue == "polymarket" else
                  f"KXHIGHNY-{day.strftime('%y%b%d').upper()}-T{79 + index}")
        rows.append({"_platform": venue, "ticker": ticker, "title": title,
                     "market_date": target, "close_time": close.isoformat(),
                     "city_hint": "New York", "strike_type": kind,
                     "floor_strike": 80, "ceiling_strike": 80,
                     "rules_primary": ("Daily NWS CLI report at KNYC" if venue == "polymarket" else
                                       "Daily maximum at CLINYC according to The Weather Company"),
                     "metadata": {"private_unused_field": "DO_NOT_EXPORT_SECRET"}})
    return rows


def capture(rows, *, errors=None):
    return {"capture_id": "b6b1d9be-b935-424d-b610-e30633321dd8",
            "captured_at": "2026-09-13T12:08:00Z",
            "market_capture": {"markets": rows, "stats": {"venue_errors": errors or {}}}}


class TestWeatherOpportunityDiagnostics(unittest.TestCase):
    def build(self, rows, configs=None, now=NOW):
        return diagnostics.build_discovery_diagnostics(capture(rows), configs or [deepcopy(CONFIG)], now=now)

    def test_missing_capture_is_unknown_not_zero_opportunities(self):
        with patch.object(diagnostics, "normalize_market") as parser:
            result = diagnostics.build_discovery_diagnostics({}, [CONFIG], now=NOW)
        parser.assert_not_called()
        self.assertEqual(result["status"], "not_captured")
        self.assertIsNone(result["summary"])
        self.assertIsNone(result["normalization"])
        self.assertFalse(result["live_ready"])

    def test_empty_and_failed_discovery_are_distinct_and_sanitized(self):
        empty = self.build([])
        self.assertEqual(empty["status"], "empty_capture")
        shared = capture([], errors={"kalshi": "https://private.invalid?token=DO_NOT_EXPORT_SECRET"})
        failed = diagnostics.build_discovery_diagnostics(shared, [CONFIG], now=NOW)
        self.assertEqual(failed["status"], "incomplete_capture")
        self.assertEqual(failed["discovery"]["failed_venues"], ["kalshi"])
        self.assertNotIn("DO_NOT_EXPORT_SECRET", json.dumps(failed, allow_nan=False))
        shared["market_capture"]["markets"] = markets()
        partial = diagnostics.build_discovery_diagnostics(shared, [CONFIG], now=NOW)
        self.assertEqual(partial["status"], "incomplete_capture")
        self.assertEqual(partial["summary"]["complete_partitions"], 1)

    def test_actual_normalization_preserves_venue_authority_and_reporting_day(self):
        result = self.build(markets() + markets("kalshi"))
        self.assertEqual(result["summary"], {"groups": 2, "complete_partitions": 2, "invalid_partitions": 0})
        self.assertEqual({g["settlement_source"] for g in result["groups"]}, {"NWS CLI", "The Weather Company"})
        for group in result["groups"]:
            self.assertEqual(group["station"], "KNYC")
            self.assertEqual(group["reporting_timezone"], "Etc/GMT+5")
            self.assertEqual(group["reporting_window_utc"], ["2026-09-13T05:00:00+00:00", "2026-09-14T05:00:00+00:00"])
            self.assertTrue(group["eligibility"][CONFIG["id"]]["eligible_as_of"])
            self.assertEqual(group["partition"]["buckets"][0]["low_f"], "-inf")
            self.assertEqual(group["partition"]["buckets"][-1]["high_f"], "+inf")
        self.assertEqual(result["experiments"][CONFIG["id"]]["eligible_complete_groups"], 2)
        self.assertNotIn("DO_NOT_EXPORT_SECRET", json.dumps(result, allow_nan=False))

    def test_missing_tails_gap_overlap_and_duplicates_are_explicit(self):
        original = markets()
        gap, overlap = deepcopy(original), deepcopy(original)
        gap[1].update(floor_strike=82, title="New York high temperature 82 or above")
        overlap[1].update(floor_strike=79, title="New York high temperature 79 or above")
        cases = [(original[1:], "MISSING_LOWER_TAIL"), (original[:1], "MISSING_UPPER_TAIL"),
                 (gap, "GAP"), (overlap, "OVERLAP"), (original + [original[0]], "OVERLAP")]
        for rows, expected in cases:
            with self.subTest(expected=expected, rows=len(rows)):
                result = self.build(rows)
                partition = result["groups"][0]["partition"]
                self.assertFalse(partition["complete"])
                self.assertIn(expected, {issue["reason"] for issue in partition["issues"]})
                self.assertEqual(result["experiments"][CONFIG["id"]]["eligible_invalid_groups"], 1)
        duplicate = self.build(original + [original[0]])["groups"][0]["partition"]
        self.assertEqual(duplicate["duplicate_market_ids"], [original[0]["ticker"]])

    def test_historical_reject_is_distinguished_from_eligible_reject_per_config(self):
        rows = markets(target="2026-09-12")[:1] + markets()[:1]
        other = {**CONFIG, "id": "weather_different_universe_v2", "stations": ["KDEN"]}
        result = self.build(rows, [CONFIG, other])
        summary = result["experiments"][CONFIG["id"]]
        self.assertEqual(summary["eligible_invalid_groups"], 1)
        self.assertEqual(summary["outside_scope_invalid_groups"], 1)
        self.assertEqual(summary["outside_scope_reasons"], {"OUTSIDE_WEATHER_EXPERIMENT_HORIZON": 1})
        self.assertEqual(result["experiments"][other["id"]]["outside_scope_invalid_groups"], 2)
        later = self.build(rows, now=NOW + timedelta(hours=1))
        self.assertEqual(later["experiments"][CONFIG["id"]]["eligible_invalid_groups"], 0)
        self.assertIn("not_individual_execution_time", later["eligibility_basis"])

    def test_station_date_metric_source_and_reporting_window_do_not_merge(self):
        rows = markets("kalshi")
        nws = deepcopy(rows)
        for row in nws:
            row["rules_primary"] = "NWS daily CLI report at KNYC"
        low = deepcopy(rows)
        for row in low:
            row["metric_hint"] = "low"
        result = self.build(rows + nws + low + markets("kalshi", "2026-09-14"))
        self.assertEqual(result["summary"]["groups"], 4)
        self.assertTrue(all(g["partition"]["complete"] for g in result["groups"]))
        events = [normalize_market(row, "kalshi", require_verified_station=True) for row in rows]
        shifted = [replace(e, observation_window=tuple(t + timedelta(hours=1) for t in e.observation_window)) for e in events]
        with patch.object(diagnostics, "normalize_market", side_effect=events + shifted):
            result = self.build(rows + rows)
        self.assertEqual(result["summary"]["groups"], 2)

    def test_station_rejections_parse_skips_and_unexpected_errors_stay_separate(self):
        unsupported, skipped = markets()[0], markets()[0]
        unsupported["rules_primary"] = "Daily NWS report at KXYZ"
        skipped.update(strike_type="unparseable", title="No bucket in this label")
        result = self.build([unsupported, skipped])
        normal = result["normalization"]
        self.assertEqual((normal["station_rejected_rows"], normal["skipped_rows"], normal["error_rows"]), (1, 1, 0))
        self.assertIn("UNSUPPORTED_SETTLEMENT_STATION", normal["reasons"])
        self.assertEqual(result["summary"]["invalid_partitions"], 0)
        with patch.object(diagnostics, "normalize_market", side_effect=ValueError("DO_NOT_EXPORT_SECRET")):
            error = self.build(markets())
        self.assertEqual(error["normalization"]["error_rows"], 2)
        self.assertNotIn("DO_NOT_EXPORT_SECRET", json.dumps(error))

    def test_json_safe_nonfinite_bounds_and_untrusted_identifiers(self):
        rows = markets()
        rows[1]["floor_strike"] = float("nan")
        result = self.build(rows)
        self.assertIn("INVALID_NUMERIC_BOUND", {i["reason"] for i in result["groups"][0]["partition"]["issues"]})
        json.dumps(result, allow_nan=False)
        rows = markets()
        rows[0]["ticker"] = "https://private.invalid?token=DO_NOT_EXPORT_SECRET"
        result = self.build(rows)
        self.assertTrue(result["groups"][0]["partition"]["buckets"][0]["market_id_omitted"])
        self.assertNotIn("DO_NOT_EXPORT_SECRET", json.dumps(result, allow_nan=False))

    def test_no_fetch_or_mutation_even_if_parser_changes_its_argument(self):
        shared, configs = capture(markets()), [deepcopy(CONFIG)]
        before, config_before = deepcopy(shared), deepcopy(configs)
        parser = diagnostics.normalize_market
        def mutating_parser(raw, *args, **kwargs):
            raw["metadata"]["private_unused_field"] = "changed by parser"
            return parser(raw, *args, **kwargs)
        with patch.object(diagnostics, "normalize_market", side_effect=mutating_parser), \
             patch("socket.create_connection", side_effect=AssertionError("unexpected network")), \
             patch("builtins.open", side_effect=AssertionError("unexpected file I/O")):
            result = diagnostics.build_discovery_diagnostics(shared, configs, now=NOW)
        self.assertEqual(shared, before)
        self.assertEqual(configs, config_before)
        self.assertEqual(result["normalization"]["normalized_rows"], 2)
        self.assertFalse(result["live_ready"])

    def test_invalid_capture_config_and_time_are_visible_without_raw_data(self):
        invalid = diagnostics.build_discovery_diagnostics({"market_capture": {"markets": "secret"}}, [CONFIG], now=NOW)
        self.assertEqual(invalid["status"], "invalid_capture")
        result = self.build([{ "_platform": ["bad"]}, None], [{**CONFIG, "max_lead_days": "secret"}])
        self.assertEqual(result["status"], "diagnostic_error")
        self.assertTrue(result["config_errors"])
        self.assertNotIn("secret", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "DIAGNOSTIC_TIME_MUST_BE_TIMEZONE_AWARE"):
            self.build(markets(), now=NOW.replace(tzinfo=None))

    def test_provenance_and_unknown_eligibility_cannot_look_complete(self):
        shared = capture(markets())
        shared.update(capture_id="-" * 36, captured_at="DO_NOT_EXPORT_SECRET")
        with patch.object(diagnostics, "event_scope_reason", side_effect=RuntimeError("DO_NOT_EXPORT_SECRET")):
            result = diagnostics.build_discovery_diagnostics(shared, [CONFIG], now=NOW)
        self.assertEqual(result["status"], "diagnostic_error")
        self.assertEqual(result["capture_warnings"], ["CAPTURE_ID_UNAVAILABLE", "CAPTURED_AT_UNAVAILABLE"])
        self.assertIsNone(result["groups"][0]["eligibility"][CONFIG["id"]]["eligible_as_of"])
        self.assertEqual(result["experiments"][CONFIG["id"]]["eligibility_unknown_groups"], 1)
        self.assertNotIn("DO_NOT_EXPORT_SECRET", json.dumps(result))

    def test_diagnostic_files_do_not_change_frozen_implementation_identity(self):
        self.assertNotIn("backend/trading/weather_opportunity_diagnostics.py", IMPLEMENTATION_FILES)
        self.assertEqual(implementation_hash(), "a1480c622eea39e396142727d02d4efbf5486786984daff44a65d49221da71b3")


if __name__ == "__main__":
    unittest.main()
