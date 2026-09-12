"""Regression tests for contract-station research, separate from frozen v1.

Fixture wording mirrors public Sep 12 contracts inspected on Sep 12, 2026:
Kalshi /trade-api/v2/markets?series_ticker=... and Polymarket US
https://gateway.polymarket.us/v1/search?query=temperature&limit=20.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from pavlov.pipeline.settlement_resolver import normalize_market
from pavlov.pipeline.station_mapper import (
    STATION_MAP, StationMappingError, get_station_metadata, resolve_market_station,
)


def market(**overrides):
    return {
        "ticker": "KXHIGHCHI-26SEP12-T91", "city_hint": "Chicago",
        "title": "Will the maximum temperature be >91° on Sep 12, 2026?",
        "strike_type": "greater", "floor_strike": 91, "market_date": "2026-09-12",
        "close_time": "2026-09-13T06:00:00Z",
        "rules_primary": "If the maximum temperature recorded at Chicago (CLIMDW) "
                         "for Sep 12, 2026, is greater than 91° fahrenheit according "
                         "to The Weather Company, then the market resolves to Yes.",
        **overrides,
    }


class TestVerifiedStationRouting(unittest.TestCase):
    def test_actual_kalshi_midway_contract_uses_midway_and_actual_authority(self):
        event = normalize_market(market(), "kalshi", require_verified_station=True)
        self.assertEqual(event.settlement_station, "KMDW")
        self.assertEqual(event.settlement_source, "The Weather Company")
        self.assertEqual(event.local_timezone, "America/Chicago")
        self.assertEqual(event.observation_timezone, "Etc/GMT+6")
        self.assertEqual(event.observation_window[0].isoformat(), "2026-09-12T06:00:00+00:00")
        self.assertEqual(event.observation_window[1].isoformat(), "2026-09-13T06:00:00+00:00")
        self.assertTrue(event.station_verified)

    def test_actual_poly_midway_description_is_independent_of_kalshi_series(self):
        row = market(ticker="tc-temp-mdwhigh-2026-09-12-gt91f", rules_primary="",
                     description="Will the highest temperature recorded at Chicago Midway Airport "
                     "(KMDW) in Chicago for 2026-09-12 as reported by the National Weather "
                     "Service's Climatological Report (Daily) be greater than 91F?")
        event = normalize_market(row, "polymarket", require_verified_station=True)
        self.assertEqual(event.settlement_station, "KMDW")
        self.assertEqual(event.settlement_source, "NWS CLI")
        self.assertEqual(event.observation_timezone, "Etc/GMT+6")

    def test_kalshi_civil_midnight_close_conflicts_with_daily_standard_window(self):
        with self.assertRaisesRegex(StationMappingError, "CONFLICTING_OBSERVATION_DAY"):
            normalize_market(market(close_time="2026-09-13T05:00:00Z"), "kalshi", require_verified_station=True)

    def test_unknown_reporting_authority_cannot_imply_a_day_window(self):
        with self.assertRaisesRegex(StationMappingError, "UNVERIFIED_OBSERVATION_DAY"):
            normalize_market(market(ticker="poly", rules_primary="Recorded at KMDW by an unspecified source"),
                             "polymarket", require_verified_station=True)

    def test_poly_ohare_contract_remains_distinct(self):
        row = market(ticker="poly-custom", rules_primary="", settlement_station="KORD")
        result = resolve_market_station(row, "polymarket")
        self.assertEqual(result.station, "KORD")
        self.assertNotEqual(get_station_metadata("KORD")["lat"], get_station_metadata("KMDW")["lat"])

    def test_supported_series_does_not_replace_missing_contract_station(self):
        with self.assertRaisesRegex(StationMappingError, "MISSING_EXPLICIT_SETTLEMENT_STATION"):
            normalize_market(market(rules_primary=""), "kalshi", require_verified_station=True)

    def test_conflicting_explicit_stations_fail_closed(self):
        with self.assertRaisesRegex(StationMappingError, "CONFLICTING_SETTLEMENT_STATIONS"):
            normalize_market(market(settlement_station="KORD"), "kalshi", require_verified_station=True)

    def test_known_series_disagrees_with_rules(self):
        with self.assertRaisesRegex(StationMappingError, "CONFLICTING_SERIES_SETTLEMENT_STATION"):
            resolve_market_station(market(rules_primary="Recorded at KORD"), "kalshi")

    def test_unknown_station_never_falls_back_to_city(self):
        for row in (market(rules_primary="Recorded at KXYZ"),
                    market(rules_primary="Recorded at CLIXXX"),
                    market(metadata={"settlement_station": "XYZ"})):
            with self.subTest(row=row), self.assertRaisesRegex(StationMappingError, "UNSUPPORTED_SETTLEMENT_STATION"):
                resolve_market_station(row, "kalshi")

    def test_wrong_city_hint_fails_closed(self):
        with self.assertRaisesRegex(StationMappingError, "CONFLICTING_SETTLEMENT_CITY"):
            resolve_market_station(market(city_hint="Miami"), "kalshi")

    def test_verified_city_and_timezone_come_from_station_not_title(self):
        row = market(ticker="poly", city_hint="", title="Temperature >91",
                     rules_primary="NWS report at KLAX")
        event = normalize_market(row, "polymarket", require_verified_station=True)
        self.assertEqual((event.city, event.local_timezone), ("Los Angeles", "America/Los_Angeles"))

    def test_newly_vetted_series_resolve_from_explicit_rules(self):
        for series, cli, station in (("KXHIGHDEN", "CLIDEN", "KDEN"),
                                     ("KXHIGHPHIL", "CLIPHL", "KPHL"),
                                     ("KXHIGHAUS", "CLIAUS", "KAUS"),
                                     ("KXHIGHTSFO", "CLISFO", "KSFO")):
            with self.subTest(series=series):
                result = resolve_market_station(market(ticker=series + "-26SEP12-T91",
                    city_hint="", rules_primary=f"Recorded at ({cli}) according to The Weather Company"), "kalshi")
                self.assertEqual(result.station, station)

    def test_legacy_mapping_and_source_preserved(self):
        event = normalize_market(market(), "kalshi")
        self.assertEqual(event.settlement_station, "KORD")
        self.assertEqual(event.settlement_source, "NWS CLI")
        self.assertFalse(event.station_verified)
        self.assertEqual(STATION_MAP["Chicago"]["lat"], 41.9742)


class TestStationEnsemble(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pipeline_root = str(Path(__file__).resolve().parents[2] / "pavlov")
        if pipeline_root not in sys.path:
            sys.path.insert(0, pipeline_root)
        with mock.patch.dict(os.environ, {"PAVLOV_BYPASS_CONFIG": "1"}):
            from pavlov.pipeline import ensemble_client
        cls.client = ensemble_client

    def test_forecast_location_and_bias_are_station_specific(self):
        client = self.client
        with mock.patch.object(client, "_load_cache", return_value={}), \
             mock.patch.object(client, "_save_cache") as save, \
             mock.patch.object(client, "_active_models", return_value={"gfs025": 1}), \
             mock.patch.object(client, "_BIAS", {"Chicago": 12, "KORD:high": 8}), \
             mock.patch.object(client, "_fetch_model", return_value={"2026-09-12": [70, 71, 72]}) as fetch:
            self.assertIsNotNone(client._fetch_members("Chicago", "high", settlement_station="KMDW"))
        fetch.assert_called_once_with(41.78417, -87.75528, "temperature_2m_max", "gfs025", 0.0,
                                      local_timezone="Etc/GMT+6")
        self.assertIn(client._cache_key("Chicago", "high", "KMDW"), save.call_args.args[0])
        self.assertNotEqual(client._cache_key("Chicago", "high"), client._cache_key("Chicago", "high", "KMDW"))
        self.assertNotEqual(client._cache_key("Chicago", "high", "KORD"), client._cache_key("Chicago", "high", "KMDW"))
        self.assertNotEqual(client._cache_key("Chicago", "high", "KMDW", "America/Chicago"),
                            client._cache_key("Chicago", "high", "KMDW", "Etc/GMT+6"))

    def test_unknown_or_inconsistent_station_never_fetches(self):
        with mock.patch.object(self.client, "_fetch_model") as fetch:
            self.assertIsNone(self.client._fetch_members("Chicago", "high", settlement_station="KXYZ"))
            self.assertIsNone(self.client._fetch_members("Miami", "high", settlement_station="KMDW"))
        fetch.assert_not_called()

    def test_daily_api_request_uses_station_local_day(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"daily": {"time": ["2026-09-12"],
                                                "temperature_2m_max_member01": [80]}}
        with mock.patch.object(self.client, "_om_rate_limit"), \
             mock.patch.object(self.client.requests, "get", return_value=response) as get:
            self.client._fetch_model(41.78417, -87.75528, "temperature_2m_max", "gfs025", 0,
                                     local_timezone="Etc/GMT+6")
        query = parse_qs(urlparse(get.call_args.args[0]).query)
        self.assertEqual(query["timezone"], ["Etc/GMT+6"])
        self.assertEqual(query["latitude"], ["41.78417"])


if __name__ == "__main__":
    unittest.main()
