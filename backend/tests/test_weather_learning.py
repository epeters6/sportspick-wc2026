"""Safety and time-integrity tests for the independent station learning pipeline."""
import copy
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from backend.ml.weather_learning import SOURCE
from backend.ml.weather_learning.contracts import Contract, resolve
from backend.ml.weather_learning.collection import collect, normalized_quote
from backend.ml.weather_learning.feeds import PublicFeeds
from backend.ml.weather_learning.dataset import features, partition_valid, prepare, utc, vector_key
from backend.ml.weather_learning.nowcast import weather_features
from backend.ml.weather_learning.outcomes import final_result
from backend.ml.weather_learning.training import normalize, split_history, train, load_artifact, predict, weights


def history(days=12):
    rows = []
    start = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)
    for day in range(days):
        for station in ("KNYC", "KPHL", "KMDW", "KDEN"):
            for hour in (0, 3):
                stamp = start + timedelta(days=day, hours=hour)
                for i, (lo, hi) in enumerate(((None, 79.5), (79.5, 81.5), (81.5, None))):
                    market = f"{station}:{day}:{i}"
                    rows.append({"id": f"{market}:{hour}", "source": "weather_forward_v2", "event_key": f"{station}:{day}",
                                 "outcome": market, "prob": (.2, .6, .2)[i], "market_price": (.25, .5, .25)[i],
                                 "created_at": stamp.isoformat(), "resolved_at": (start + timedelta(days=day + 1)).isoformat(),
                                 "is_correct": i == day % 3,
                                 "metadata": {"station": station, "station_verified": True, "platform": "kalshi", "metric": "high",
                                              "target_date": stamp.date().isoformat(), "lead_days": 0, "decision_at": stamp.isoformat(),
                                              "run_id": stamp.isoformat(), "bucket_low_f": lo, "bucket_high_f": hi,
                                              "market_vector_prob": (.25, .5, .25)[i], "raw_model_prob": (.2, .6, .2)[i],
                                              "observation_timezone": "Etc/GMT+5", "settlement_source": "NWS CLI",
                                              "label_source": "venue_official", "official_market_id": market,
                                              "official_result": "yes" if i == day % 3 else "no", "executable_cost": (.27, .52, .27)[i]}})
    return rows


def market(**changes):
    result = {"ticker": "KXHIGHPHIL-26SEP20-T75", "title": "Highest temperature in Philadelphia?",
              "rules_primary": "If the maximum temperature recorded at Philadelphia (CLIPHL) for Sep 20, 2026, is less than 75° fahrenheit according to The Weather Company, then the market resolves to Yes.",
              "close_time": "2026-09-21T05:00:00Z", "market_date": "2026-09-20", "strike_type": "less", "cap_strike": 75}
    result.update(changes)
    return result


class DatasetTests(unittest.TestCase):
    def test_only_official_complete_partitions_train(self):
        rows = history(1)
        rows[0]["metadata"]["label_source"] = "station_metar"
        kept, report = prepare(rows, utc("2026-08-01T00:00:00Z"))
        self.assertEqual(len(kept), len(rows) - 3)
        self.assertIn("LABEL_NOT_OFFICIAL_OR_NOT_AVAILABLE", report["rejected_rows_by_reason"])

    def test_partial_conflicting_or_wrong_id_labels_reject_whole_vector(self):
        for change in ("missing", "double_winner", "wrong_market", "future"):
            rows = history(1)[:3]
            if change == "missing": rows.pop()
            if change == "double_winner": rows[1]["is_correct"] = True
            if change == "wrong_market": rows[1]["metadata"]["official_market_id"] = "OTHER"
            if change == "future": rows[1]["resolved_at"] = "2030-01-01T00:00:00Z"
            self.assertFalse(prepare(rows, utc("2026-08-01T00:00:00Z"))[0])

    def test_feature_allowlist_does_not_read_labels(self):
        row = history(1)[0]
        expected = features(row)
        row["is_correct"] = not row["is_correct"]
        row["metadata"].update(official_result="no", selected=True, pnl=999, meteorological_actual_f=123)
        self.assertEqual(features(row), expected)

    def test_station_days_have_equal_total_weight(self):
        rows = history(2)
        totals = {}
        for row, weight in zip(rows, weights(rows)):
            key = (row["metadata"]["station"], row["metadata"]["target_date"])
            totals[key] = totals.get(key, 0) + weight
        self.assertAlmostEqual(min(totals.values()), max(totals.values()))

    def test_split_purges_late_labels_and_keeps_days_disjoint(self):
        rows = history()
        for row in rows:
            # Some forecasts were issued the preceding day.
            if row["metadata"]["station"] == "KNYC":
                stamp = (utc(row["created_at"]) - timedelta(days=1)).isoformat()
                row["created_at"] = row["metadata"]["decision_at"] = stamp
        parts = split_history(rows)
        for left, right in zip(parts, parts[1:]):
            self.assertLess(max(utc(r["resolved_at"]) for r in left), min(utc(r["created_at"]) for r in right))
            self.assertFalse({r["metadata"]["target_date"] for r in left} & {r["metadata"]["target_date"] for r in right})

    def test_group_normalization_and_training_artifact(self):
        rows = history()
        p = normalize(rows, np.full(len(rows), .8))
        for i in range(0, len(p), 3):
            self.assertAlmostEqual(sum(p[i:i + 3]), 1)
        with tempfile.TemporaryDirectory() as directory:
            report = train(rows, Path(directory), as_of=utc("2026-08-01T00:00:00Z"))
            self.assertEqual(report["status"], "trained_research_only")
            self.assertFalse(report["live_ready"])
            artifact = load_artifact(Path(directory))
            with self.assertRaisesRegex(ValueError, "TRAINED_AFTER_DECISION"):
                predict(rows[:3], artifact)
            path = Path(directory) / "model.joblib"
            path.write_bytes(path.read_bytes() + b"corruption")
            with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
                load_artifact(Path(directory))


class ContractTests(unittest.TestCase):
    def test_station_source_and_standard_time_are_preserved(self):
        contract = resolve(market(), "kalshi")
        self.assertEqual(contract.station, "KPHL")
        self.assertEqual(contract.authority, "The Weather Company")
        self.assertEqual(contract.window_start, "2026-09-20T05:00:00+00:00")
        self.assertEqual(contract.high_f, 74.5)

    def test_city_title_cannot_supply_station(self):
        with self.assertRaisesRegex(ValueError, "SETTLEMENT_STATION"):
            resolve(market(rules_primary="The maximum temperature in Philadelphia in fahrenheit according to The Weather Company"), "kalshi")

    def test_wrong_airport_conflicts_and_wrong_window_rejected(self):
        with self.assertRaisesRegex(ValueError, "CONFLICTING_SETTLEMENT_STATION"):
            resolve(market(settlement_station="KNYC"), "kalshi")
        with self.assertRaisesRegex(ValueError, "OBSERVATION_WINDOW"):
            resolve(market(close_time="2026-09-21T04:00:00Z"), "kalshi")

    def test_generic_maximum_minimum_boilerplate_does_not_change_metric(self):
        contract = resolve(market(rules_secondary="The maximum/minimum temperature as reported by the Weather Company."), "kalshi")
        self.assertEqual(contract.metric, "high")

    def test_negative_temperature_thresholds(self):
        row = market(cap_strike=-5, rules_primary=market()["rules_primary"].replace("75°", "-5°"))
        self.assertEqual(resolve(row, "kalshi").high_f, -5.5)

    def test_expansion_requires_exact_new_station_evidence(self):
        row = market(rules_primary="The maximum temperature recorded at Phoenix (CLIPHX) is less than 75 degrees fahrenheit according to The Weather Company",
                     close_time="2026-09-21T07:00:00Z")
        self.assertEqual(resolve(row, "kalshi").station, "KPHX")

    def test_houston_hobby_is_not_bush_intercontinental(self):
        row = market(rules_primary="The maximum temperature recorded at Houston (CLIHOU) is less than 75 degrees fahrenheit according to The Weather Company",
                     close_time="2026-09-21T06:00:00Z")
        self.assertEqual(resolve(row, "kalshi").station, "KHOU")

    def test_polymarket_explicit_f_units_inclusive_and_negative_bounds(self):
        base = {"ticker": "tc-temp-nychigh-2026-09-20-lt71f", "market_date": "2026-09-20"}
        prefix = "Will the highest temperature recorded at Central Park (KNYC) as reported by the National Weather Service's Climatological Report (Daily) be "
        for clause, fields, expected in (
            ("less than or equal to -5F?", {"ceiling_strike": -5}, (None, -4.5)),
            ("between -4F and -3F?", {"threshold_lo": -4, "threshold_hi": -3}, (-4.5, -2.5)),
            ("greater than or equal to -2F?", {"floor_strike": -2}, (-2.5, None)),
        ):
            row = {**base, **fields, "description": prefix + clause}
            contract = resolve(row, "polymarket")
            self.assertEqual((contract.low_f, contract.high_f), expected)
        with self.assertRaisesRegex(ValueError, "RULE_STRIKE_MISMATCH"):
            resolve({**base, "floor_strike": 80, "description": prefix + "greater than or equal to 79F?"}, "polymarket")


class OutcomeTests(unittest.TestCase):
    def test_only_exact_final_binary_payouts_become_labels(self):
        payload = {"ticker": "M", "status": "finalized", "result": "yes", "settlement_value_dollars": "1.0000"}
        self.assertEqual(final_result("kalshi", "M", payload), "yes")
        for patch_value in ({"ticker": "OTHER"}, {"status": "determined"}, {"settlement_value_dollars": ".5"},
                            {"settlement_value_dollars": "0"}, {"settlement_value_dollars": True}):
            self.assertIsNone(final_result("kalshi", "M", {**payload, **patch_value}))

    def test_polymarket_float_one_is_a_win_and_refunds_are_excluded(self):
        self.assertEqual(final_result("polymarket", "M", {"slug": "M", "settlement": 1.0}), "yes")
        for value in (.5, True, None, "1.0000"):
            self.assertIsNone(final_result("polymarket", "M", {"slug": "M", "settlement": value}))


class NowcastTests(unittest.TestCase):
    def bundle(self, hour=21):
        contract = resolve(market(), "kalshi")
        start = utc(contract.window_start)
        times = [(start + timedelta(hours=i)).timestamp() for i in range(24)]
        temps = [70.0] * 24
        temps[9] = 95.0  # An earlier daily peak must not disappear at night.
        hourly = {"time": times}
        for model in ("gfs_global", "gfs_hrrr", "ecmwf_ifs025"):
            hourly["temperature_2m_" + model] = temps
            hourly["cloud_cover_" + model] = [25] * 24
        decision = start + timedelta(hours=hour)
        forecast = {"received_at": decision.isoformat(), "data": {"hourly": hourly}}
        obs = {"received_at": decision.isoformat(), "data": []}
        return contract, forecast, obs, decision

    def test_missing_observations_do_not_collapse_high_to_evening_temp(self):
        args = self.bundle()
        f = weather_features(*args)
        self.assertEqual(f["nowcast_mean_f"], 95)
        self.assertEqual(f["observation_count"], 0)

    def test_same_day_high_preserves_observed_peak(self):
        contract, forecast, obs, decision = self.bundle()
        obs["data"] = [{"icaoId": "KPHL", "reportTime": (decision - timedelta(hours=10)).isoformat(), "temp": 35},
                       {"icaoId": "KPHL", "reportTime": decision.isoformat(), "temp": 22}]
        f = weather_features(contract, forecast, obs, decision)
        self.assertGreaterEqual(f["nowcast_mean_f"], 95)
        self.assertEqual(f["model_count"], 3)

    def test_wrong_station_future_observations_and_stale_data_not_used(self):
        contract, forecast, obs, decision = self.bundle()
        obs["data"] = [{"icaoId": "KNYC", "reportTime": decision.isoformat(), "temp": 45},
                       {"icaoId": "KPHL", "reportTime": (decision + timedelta(hours=1)).isoformat(), "temp": 45},
                       {"icaoId": "KPHL", "reportTime": (decision - timedelta(hours=3)).isoformat(), "temp": 20}]
        f = weather_features(contract, forecast, obs, decision)
        self.assertEqual(f["observation_count"], 1)
        self.assertNotIn("observed_temp_f", f)
        self.assertEqual(f["nowcast_mean_f"], 95)

    def test_no_future_receipts_and_no_incomplete_day(self):
        contract, forecast, obs, decision = self.bundle()
        with self.assertRaisesRegex(ValueError, "RECEIVED_AFTER_DECISION"):
            weather_features(contract, forecast, obs, decision - timedelta(seconds=1))
        forecast["data"]["hourly"]["time"].pop()
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_FORECAST"):
            weather_features(contract, forecast, obs, decision)


class CollectionTests(unittest.TestCase):
    def test_cents_are_unambiguous_including_one_cent(self):
        self.assertEqual(normalized_quote({"yes_bid": 0, "yes_ask": 1})["best_ask"], .01)
        self.assertEqual(normalized_quote({"yes_ask": 75, "best_ask": .01})["best_ask"], .01)
        with self.assertRaisesRegex(ValueError, "CROSSED_QUOTE"):
            normalized_quote({"yes_bid": 20, "yes_ask": 10})

    def test_complete_collection_keeps_original_evidence_and_rejects_stale_quotes(self):
        contract, forecast, obs, decision = NowcastTests().bundle(hour=10)  # 11am EDT
        forecast["content_hash"] = "forecast-hash"
        obs["content_hash"] = "observation-hash"
        low = market(yes_bid=1, yes_ask=2, received_timestamp=decision.isoformat(), _platform="kalshi")
        high = market(ticker="HIGH", strike_type="greater", floor_strike=74, cap_strike=None,
                      rules_primary=market()["rules_primary"].replace("less than 75", "greater than 74"),
                      yes_bid=97, yes_ask=98, received_timestamp=decision.isoformat(), _platform="kalshi")
        feeds = Mock()
        feeds.observations.return_value = obs
        feeds.forecast.return_value = forecast
        feeds.station.return_value = {"station": "KPHL", "lat": 39.87, "lon": -75.24, "elevation_m": 11}
        with tempfile.TemporaryDirectory() as directory, patch("backend.ml.weather_learning.collection.refresh_quotes", return_value=[low, high]):
            output = Path(directory)
            report = collect(output, feeds=feeds, markets=[low, high], clock=lambda: decision)
            self.assertEqual(report["snapshots"], 2, report)
            saved = [json.loads(line) for line in (output / "snapshots.jsonl").read_text().splitlines()]
            self.assertAlmostEqual(sum(r["prob"] for r in saved), 1)
            self.assertEqual(saved[0]["market_price"], .02)
            self.assertEqual(saved[0]["metadata"]["weather_forecast_hash"], "forecast-hash")
            self.assertFalse(saved[0]["metadata"]["execution_enabled"])
            self.assertIsNone(saved[0]["is_correct"])
            low["received_timestamp"] = (decision - timedelta(minutes=4)).isoformat()
            report = collect(output, feeds=feeds, markets=[low, high], clock=lambda: decision)
            self.assertEqual(report["snapshots"], 0)
            self.assertEqual(report["errors"][0]["reason"], "STALE_QUOTE_SNAPSHOT")

    def test_cache_preserves_receipt_and_does_not_use_future_cache(self):
        stamp = utc("2026-09-20T15:00:00Z")
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"value": 1}
        with tempfile.TemporaryDirectory() as directory, patch("backend.ml.weather_learning.feeds.time.sleep"):
            feeds = PublicFeeds(Path(directory), session=session, clock=lambda: stamp)
            first = feeds.fetch("https://example.invalid/weather", {}, ttl=300)
            feeds.clock = lambda: stamp + timedelta(minutes=1)
            self.assertEqual(feeds.fetch("https://example.invalid/weather", {}, ttl=300), first)
            self.assertEqual(session.get.call_count, 1)
            feeds.clock = lambda: stamp - timedelta(minutes=1)
            second = feeds.fetch("https://example.invalid/weather", {}, ttl=300)
            self.assertEqual(session.get.call_count, 2)
            self.assertNotEqual(first["received_at"], second["received_at"])


if __name__ == "__main__":
    unittest.main()
