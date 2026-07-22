"""Prove unit tests never open a real Supabase client for CLV persistence."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from backend.tests.clv_test_isolation import isolate_clv_db
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.risk_caps import RiskCaps
from backend.models.sports.sync_sports import sync_sports_market
from pavlov.pipeline.clv_tracker import init_clv_record


class TestNoExternalDbFromUnitPaths(unittest.TestCase):
    def test_sync_sports_and_init_clv_make_zero_get_db_calls(self):
        get_db_calls = []

        def tracking_get_db():
            get_db_calls.append("get_db")
            raise AssertionError("unit test must not call get_db")

        features = SportsEventFeatures(
            sport="mlb",
            league="mlb",
            event_id="e_iso",
            market_id="m1",
            team_a="A",
            team_b="B",
            start_time=datetime.now(timezone.utc) + timedelta(days=1),
            snapshot_time=datetime.now(timezone.utc),
            market_prob_baseline=0.2,
            market_price_source="test",
            elo_team_a=1500,
            elo_team_b=1500,
            elo_diff=10000,
            consensus_pick_count_a=0,
            consensus_pick_count_b=0,
            consensus_weighted_signal=0.0,
            source_clv_weighted_signal=0.0,
            source_count=0,
            independent_source_count=0,
            sport_specific={},
        )
        caps = RiskCaps(
            max_event_exposure_pct=0.05,
            max_outcome_exposure_pct=0.02,
            max_strategy_exposure_pct=0.1,
            max_platform_exposure_pct=0.2,
            max_daily_loss_pct=0.05,
            max_weekly_loss_pct=0.1,
            min_net_edge=0.0,
            min_log_growth_delta=0.0,
        )
        now = datetime.now(timezone.utc)

        with patch("backend.db.get_db", side_effect=tracking_get_db):
            with isolate_clv_db() as upsert:
                init_clv_record(
                    "iso1",
                    "m1",
                    "tok",
                    "YES",
                    0.4,
                    now,
                    entry_market_price=0.4,
                    entry_effective_cost=0.42,
                )
                sync_sports_market(
                    {"platform": "polymarket", "outcome_id": "tok"},
                    features,
                    best_ask=0.2,
                    best_bid=0.18,
                    spread=0.02,
                    fee_per_share=0.0,
                    visible_depth=100,
                    bankroll=1000,
                    risk_caps=caps,
                    mode="shadow",
                    real_orderbook_timestamp=now,
                    real_received_timestamp=now,
                    outcome_id="tok",
                )
                self.assertGreaterEqual(upsert.call_count, 1)

        self.assertEqual(
            get_db_calls,
            [],
            f"unexpected get_db calls during isolated unit path: {get_db_calls}",
        )
