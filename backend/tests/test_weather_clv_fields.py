"""Weather CLV: market fill vs effective cost + YES token outcome_id."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from backend.tests.clv_test_isolation import isolate_clv_db
from backend.models.weather.sync_weather import init_weather_clv_record


class TestWeatherClvFields(unittest.TestCase):
    def test_weather_clv_uses_fill_token_and_split_prices_without_db(self):
        get_db_calls = []

        def tracking_get_db():
            get_db_calls.append("get_db")
            raise AssertionError("unit test must not call get_db")

        fill = SimpleNamespace(simulated_fill_price=0.41, limit_price=0.47)
        raw_m = {"yes_token": "0xyes_token_abc"}

        with patch("backend.db.get_db", side_effect=tracking_get_db):
            with isolate_clv_db() as upsert:
                rec = init_weather_clv_record(
                    candidate_id="poly:KNYC:2026-07-22:high:m1:yes:paper",
                    market_id="m1",
                    raw_m=raw_m,
                    fill=fill,
                    platform="polymarket",
                )

        self.assertEqual(rec.outcome_id, "0xyes_token_abc")
        self.assertEqual(rec.entry_market_price, 0.41)
        self.assertEqual(rec.entry_effective_cost, 0.47)
        self.assertEqual(rec.entry_price, 0.41)
        self.assertNotEqual(rec.entry_market_price, rec.entry_effective_cost)
        self.assertGreaterEqual(upsert.call_count, 1)
        self.assertEqual(get_db_calls, [])

    def test_weather_clv_falls_back_outcome_id_yes(self):
        fill = SimpleNamespace(simulated_fill_price=0.3, limit_price=0.35)
        with isolate_clv_db():
            rec = init_weather_clv_record(
                candidate_id="c1",
                market_id="m1",
                raw_m={},
                fill=fill,
                platform="kalshi",
            )
        self.assertEqual(rec.outcome_id, "yes")

    def test_weather_clv_persists_decision_probability_size_and_close(self):
        fill = SimpleNamespace(
            simulated_fill_price=0.31,
            limit_price=0.35,
            filled_shares=12.0,
        )
        close = datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc)
        metadata = {
            "model_prob": 0.42,
            "raw_model_prob": 0.58,
            "market_prob": 0.31,
            "shares": 12.0,
            "stake": 4.2,
        }
        with isolate_clv_db() as upsert:
            init_weather_clv_record(
                candidate_id="kalshi:KNYC:2026-08-01:low:m1:yes:paper",
                market_id="m1",
                raw_m={},
                fill=fill,
                platform="kalshi",
                due_close=close - timedelta(minutes=5),
                metadata=metadata,
            )

        self.assertEqual(upsert.call_args.kwargs["due_close"], close - timedelta(minutes=5))
        self.assertEqual(upsert.call_args.kwargs["metadata"]["model_prob"], 0.42)
        self.assertEqual(upsert.call_args.kwargs["metadata"]["shares"], 12.0)
