"""Phase 4 MLB moneyline matching + cross-venue regression tests."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.models.sports.mlb_moneyline_match import (
    match_mlb_moneyline_contract,
    resolve_kalshi_yes_team,
)
from backend.models.sports.run_shadow_mlb import (
    MLB_SHADOW_ZERO_PROCESSED,
    PREGAME_MODEL_UNAVAILABLE,
    report_pitcher_outs_availability,
    run_mlb_moneyline_shadow,
    run_mlb_shadow_execution,
)
from backend.tests.clv_test_isolation import isolate_clv_db


def _poly(question, outcomes, market_id="pm1", end_date="2026-07-22", **extra):
    outs = []
    for name, tok in outcomes:
        outs.append(SimpleNamespace(name=name, token_id=tok, price=0.45, best_ask=0.46))
    return SimpleNamespace(
        question=question,
        market_id=market_id,
        slug=extra.get("slug", market_id),
        end_date=end_date,
        venue="polymarket",
        outcomes=outs,
        yes_proposition_team=None,
    )


def _kalshi(title, ticker, yes_team, end_date="2026-07-22"):
    return SimpleNamespace(
        question=title,
        market_id=ticker,
        slug=ticker,
        end_date=end_date,
        venue="kalshi",
        yes_proposition_team=yes_team,
        outcomes=[
            SimpleNamespace(name="Yes", token_id="yes", price=0.55),
            SimpleNamespace(name="No", token_id="no", price=0.45),
        ],
    )


class TestMlbMoneylineMatch(unittest.TestCase):
    def test_polymarket_home_team_token_mapping(self):
        markets = [
            _poly(
                "Yankees vs Dodgers Winner 2026-07-22",
                [("New York Yankees", "tok_nyy"), ("Los Angeles Dodgers", "tok_lad")],
            )
        ]
        m = match_mlb_moneyline_contract(
            markets=markets,
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
            slate_date="2026-07-22",
            selected_team="New York Yankees",
            venue="polymarket",
        )
        self.assertIsNone(m.rejection_reason)
        self.assertEqual(m.outcome.token_id, "tok_nyy")

    def test_polymarket_away_team_token_mapping(self):
        markets = [
            _poly(
                "Yankees vs Dodgers Winner 2026-07-22",
                [("New York Yankees", "tok_nyy"), ("Los Angeles Dodgers", "tok_lad")],
            )
        ]
        m = match_mlb_moneyline_contract(
            markets=markets,
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
            slate_date="2026-07-22",
            selected_team="Los Angeles Dodgers",
            venue="polymarket",
        )
        self.assertIsNone(m.rejection_reason)
        self.assertEqual(m.outcome.token_id, "tok_lad")

    def test_kalshi_yes_team_mapping(self):
        markets = [
            _kalshi(
                "Will the Yankees win?",
                "KXMLBGAME-26JUL22NYYLAD-NYY",
                "New York Yankees",
            )
        ]
        m = match_mlb_moneyline_contract(
            markets=markets,
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
            slate_date="2026-07-22",
            selected_team="New York Yankees",
            venue="kalshi",
        )
        self.assertIsNone(m.rejection_reason)
        self.assertEqual(m.outcome.token_id, "yes")
        self.assertEqual(m.side, "YES")
        self.assertEqual(resolve_kalshi_yes_team(markets[0]), "New York Yankees")

    def test_kalshi_no_complement_mapping(self):
        markets = [
            _kalshi(
                "Will the Yankees win?",
                "KXMLBGAME-26JUL22NYYLAD-NYY",
                "New York Yankees",
            )
        ]
        m = match_mlb_moneyline_contract(
            markets=markets,
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
            slate_date="2026-07-22",
            selected_team="Los Angeles Dodgers",
            venue="kalshi",
        )
        self.assertIsNone(m.rejection_reason)
        self.assertEqual(m.outcome.token_id, "no")
        self.assertEqual(m.side, "NO")

    def test_team_direction_mismatch(self):
        markets = [
            _poly(
                "Yankees vs Dodgers Winner 2026-07-22",
                [("New York Yankees", "tok_nyy"), ("Los Angeles Dodgers", "tok_lad")],
            )
        ]
        m = match_mlb_moneyline_contract(
            markets=markets,
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
            slate_date="2026-07-22",
            selected_team="Boston Red Sox",
            venue="polymarket",
        )
        self.assertEqual(m.rejection_reason, "TEAM_DIRECTION_MISMATCH")

    def test_date_mismatch(self):
        markets = [
            _poly(
                "Yankees vs Dodgers Winner 2026-07-20",
                [("New York Yankees", "tok_nyy"), ("Los Angeles Dodgers", "tok_lad")],
                end_date="2026-07-20",
            )
        ]
        m = match_mlb_moneyline_contract(
            markets=markets,
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
            slate_date="2026-07-22",
            selected_team="New York Yankees",
            venue="polymarket",
        )
        self.assertEqual(m.rejection_reason, "DATE_MISMATCH")

    def test_ambiguous_duplicate_markets(self):
        markets = [
            _poly(
                "Yankees vs Dodgers Winner 2026-07-22",
                [("New York Yankees", "a"), ("Los Angeles Dodgers", "b")],
                market_id="m1",
            ),
            _poly(
                "Yankees vs Dodgers moneyline 2026-07-22",
                [("New York Yankees", "c"), ("Los Angeles Dodgers", "d")],
                market_id="m2",
            ),
        ]
        m = match_mlb_moneyline_contract(
            markets=markets,
            home_team="New York Yankees",
            away_team="Los Angeles Dodgers",
            slate_date="2026-07-22",
            selected_team="New York Yankees",
            venue="polymarket",
        )
        self.assertEqual(m.rejection_reason, "DUPLICATE_GAME_MARKET")


class TestKalshiDiscoveryAndTimestamps(unittest.TestCase):
    def test_no_arbitrary_team_as_series_ticker(self):
        from backend.trading.kalshi_client import KalshiClient

        client = KalshiClient()
        result = asyncio.run(
            client.fetch_markets(search="Yankees", limit=5)
        )
        self.assertEqual(result, [])

    def test_kalshi_shadow_received_timestamp_provenance(self):
        from backend.models.sports.sync_sports import sync_sports_market
        from pavlov.pipeline.sports_features import SportsEventFeatures
        from pavlov.pipeline.risk_caps import RiskCaps

        now = datetime.now(timezone.utc)
        features = SportsEventFeatures(
            sport="mlb",
            league="mlb",
            event_id="e_k",
            market_id="KXMLB-TEST",
            team_a="New York Yankees",
            team_b="Los Angeles Dodgers",
            start_time=now + timedelta(hours=5),
            snapshot_time=now,
            market_prob_baseline=0.4,
            market_price_source="test",
            elo_team_a=1500,
            elo_team_b=1500,
            elo_diff=0,
            consensus_pick_count_a=0,
            consensus_pick_count_b=0,
            consensus_weighted_signal=0.0,
            source_clv_weighted_signal=0.0,
            source_count=0,
            independent_source_count=0,
            sport_specific={
                "market_type": "moneyline",
                "strategy": "mlb_moneyline",
                "model_prob_override": 0.62,
                "coefficient_source": "mlb_quant_legacy.calculate_win_probability",
                "calibration_status": "uncalibrated_shadow",
                "outcome_token_id": "yes",
            },
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
        if os.path.exists("orderbook_snapshots.jsonl"):
            os.remove("orderbook_snapshots.jsonl")
        with isolate_clv_db():
            sync_sports_market(
                {
                    "platform": "kalshi",
                    "outcome_id": "yes",
                    "kalshi_moneyline_mapping_verified": True,
                    "allow_received_timestamp_shadow": True,
                    "timestamp_source": "received_timestamp",
                    "model_prob_override": 0.62,
                },
                features,
                best_ask=0.40,
                best_bid=0.38,
                spread=0.02,
                fee_per_share=0.01,
                visible_depth=50,
                bankroll=1000,
                risk_caps=caps,
                mode="shadow",
                real_orderbook_timestamp=None,
                real_received_timestamp=now,
                outcome_id="yes",
            )
        with open("orderbook_snapshots.jsonl") as f:
            snap = json.loads(f.readlines()[-1])
        self.assertTrue(snap["missing_orderbook_timestamp"])
        self.assertEqual(snap["timestamp_source"], "received_timestamp")
        self.assertIsNone(snap["orderbook_timestamp"])
        self.assertIsNone(snap["exchange_timestamp"])

    def test_kalshi_live_rejects_missing_exchange_timestamp(self):
        from pavlov.pipeline.order_simulator import validate_orderbook_freshness

        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError) as ctx:
            validate_orderbook_freshness(
                None,
                now,
                mode="live",
                allow_received_timestamp_for_shadow=True,
            )
        self.assertEqual(str(ctx.exception), "MISSING_ORDERBOOK_TIMESTAMP")


class TestMoneylineShadowIntegration(unittest.TestCase):
    def test_moneyline_runs_while_pitcher_outs_unavailable(self):
        report = report_pitcher_outs_availability(manifest={})
        self.assertEqual(report["rejection"], PREGAME_MODEL_UNAVAILABLE)

        future = datetime.now(timezone.utc) + timedelta(hours=8)
        slate = [
            {
                "home_team": "New York Yankees",
                "away_team": "Los Angeles Dodgers",
                "slate_date": future.strftime("%Y-%m-%d"),
                "scheduled_start_utc": future.isoformat(),
            }
        ]
        poly_mkt = _poly(
            f"Yankees vs Dodgers Winner {future.strftime('%Y-%m-%d')}",
            [("New York Yankees", "tok_home"), ("Los Angeles Dodgers", "tok_away")],
            end_date=future.strftime("%Y-%m-%d"),
        )
        book = {
            "best_bid": 0.40,
            "best_ask": 0.42,
            "ask_size": 100.0,
            "bid_size": 80.0,
            "book_timestamp": datetime.now(timezone.utc),
            "received_timestamp": datetime.now(timezone.utc),
        }
        router = MagicMock()
        router.fetch_mlb_moneyline_markets = AsyncMock(return_value=[poly_mkt])
        router.poly.fetch_markets = AsyncMock(return_value=[])
        router.get_top_of_book = AsyncMock(return_value=book)

        with isolate_clv_db(), patch(
            "backend.models.sports.run_shadow_mlb.get_mlb_quant_probability",
            return_value={"home_prob": 0.61, "away_prob": 0.39},
        ), patch(
            "backend.models.sports.run_shadow_mlb._resolve_event_times",
            return_value=(datetime.now(timezone.utc), future),
        ):
            result = asyncio.run(
                run_mlb_moneyline_shadow(
                    router=router, db=MagicMock(), bankroll=1000.0, slate=slate
                )
            )
        self.assertEqual(result["strategy"], "mlb_moneyline")
        self.assertGreaterEqual(result["slate_size"], 1)
        # Pitcher-outs unavailable must not appear as moneyline failure
        self.assertNotIn(PREGAME_MODEL_UNAVAILABLE, str(result))

    def test_best_effective_cost_venue_selection_and_dedup(self):
        future = datetime.now(timezone.utc) + timedelta(hours=8)
        slate = [
            {
                "home_team": "New York Yankees",
                "away_team": "Los Angeles Dodgers",
                "slate_date": future.strftime("%Y-%m-%d"),
                "scheduled_start_utc": future.isoformat(),
            }
        ]
        poly_mkt = _poly(
            f"Yankees vs Dodgers Winner {future.strftime('%Y-%m-%d')}",
            [("New York Yankees", "tok_home"), ("Los Angeles Dodgers", "tok_away")],
            end_date=future.strftime("%Y-%m-%d"),
        )
        kalshi_mkt = _kalshi(
            "Will the Yankees win?",
            f"KXMLBGAME-{future.strftime('%y%b%d').upper()}NYYLAD-NYY",
            "New York Yankees",
            end_date=future.strftime("%Y-%m-%d"),
        )
        now = datetime.now(timezone.utc)

        async def tob(venue, token_id, market_id):
            if venue == "polymarket":
                return {
                    "best_bid": 0.50,
                    "best_ask": 0.55,
                    "ask_size": 100.0,
                    "book_timestamp": now,
                    "received_timestamp": now,
                }
            return {
                "best_bid": 0.40,
                "best_ask": 0.42,
                "ask_size": 100.0,
                "book_timestamp": None,
                "received_timestamp": now,
                "timestamp_source": "received_timestamp",
                "missing_orderbook_timestamp": True,
            }

        router = MagicMock()
        router.fetch_mlb_moneyline_markets = AsyncMock(
            return_value=[poly_mkt, kalshi_mkt]
        )
        router.poly.fetch_markets = AsyncMock(return_value=[])
        router.get_top_of_book = AsyncMock(side_effect=tob)
        sync_calls = []

        def capture_sync(*args, **kwargs):
            sync_calls.append(kwargs.get("market_data") or args[0])

        with isolate_clv_db(), patch(
            "backend.models.sports.run_shadow_mlb.get_mlb_quant_probability",
            return_value={"home_prob": 0.70, "away_prob": 0.30},
        ), patch(
            "backend.models.sports.run_shadow_mlb._resolve_event_times",
            return_value=(now, future),
        ), patch(
            "backend.models.sports.run_shadow_mlb.sync_sports_market",
            side_effect=capture_sync,
        ):
            asyncio.run(
                run_mlb_moneyline_shadow(
                    router=router, db=MagicMock(), bankroll=1000.0, slate=slate
                )
            )
        self.assertEqual(len(sync_calls), 1)
        self.assertEqual(sync_calls[0]["platform"], "kalshi")

    def test_zero_get_db_in_isolated_moneyline_path(self):
        get_db_calls = []

        def boom():
            get_db_calls.append(1)
            raise AssertionError("no get_db")

        future = datetime.now(timezone.utc) + timedelta(hours=8)
        slate = [
            {
                "home_team": "New York Yankees",
                "away_team": "Los Angeles Dodgers",
                "slate_date": future.strftime("%Y-%m-%d"),
                "scheduled_start_utc": future.isoformat(),
            }
        ]
        poly_mkt = _poly(
            f"Yankees vs Dodgers Winner {future.strftime('%Y-%m-%d')}",
            [("New York Yankees", "tok_home"), ("Los Angeles Dodgers", "tok_away")],
            end_date=future.strftime("%Y-%m-%d"),
        )
        now = datetime.now(timezone.utc)
        router = MagicMock()
        router.fetch_mlb_moneyline_markets = AsyncMock(return_value=[poly_mkt])
        router.poly.fetch_markets = AsyncMock(return_value=[])
        router.get_top_of_book = AsyncMock(
            return_value={
                "best_bid": 0.4,
                "best_ask": 0.42,
                "ask_size": 50.0,
                "book_timestamp": now,
                "received_timestamp": now,
            }
        )
        with patch("backend.db.get_db", side_effect=boom), isolate_clv_db(), patch(
            "backend.models.sports.run_shadow_mlb.get_mlb_quant_probability",
            return_value={"home_prob": 0.6, "away_prob": 0.4},
        ), patch(
            "backend.models.sports.run_shadow_mlb._resolve_event_times",
            return_value=(now, future),
        ):
            asyncio.run(
                run_mlb_moneyline_shadow(
                    router=router, db=MagicMock(), bankroll=1000.0, slate=slate
                )
            )
        self.assertEqual(get_db_calls, [])

    def test_no_live_order_submission_in_runner(self):
        with open(
            os.path.join(
                os.path.dirname(__file__), "../models/sports/run_shadow_mlb.py"
            ),
            "r",
            encoding="utf-8",
        ) as f:
            content = f.read()
        self.assertIn('mode="shadow"', content)
        self.assertNotIn('mode="live"', content)
        self.assertNotIn("place_order", content)

    def test_bid_ask_depth_propagated(self):
        future = datetime.now(timezone.utc) + timedelta(hours=8)
        slate = [
            {
                "home_team": "New York Yankees",
                "away_team": "Los Angeles Dodgers",
                "slate_date": future.strftime("%Y-%m-%d"),
                "scheduled_start_utc": future.isoformat(),
            }
        ]
        poly_mkt = _poly(
            f"Yankees vs Dodgers Winner {future.strftime('%Y-%m-%d')}",
            [("New York Yankees", "tok_home"), ("Los Angeles Dodgers", "tok_away")],
            end_date=future.strftime("%Y-%m-%d"),
        )
        now = datetime.now(timezone.utc)
        captured = {}

        def capture_sync(market_data, features, best_ask, fee_per_share, visible_depth, bankroll, risk_caps, **kw):
            captured.update(
                {
                    "best_ask": best_ask,
                    "best_bid": kw.get("best_bid"),
                    "spread": kw.get("spread"),
                    "visible_depth": visible_depth,
                    "outcome_id": kw.get("outcome_id"),
                }
            )

        router = MagicMock()
        router.fetch_mlb_moneyline_markets = AsyncMock(return_value=[poly_mkt])
        router.poly.fetch_markets = AsyncMock(return_value=[])
        router.get_top_of_book = AsyncMock(
            return_value={
                "best_bid": 0.41,
                "best_ask": 0.44,
                "ask_size": 77.0,
                "book_timestamp": now,
                "received_timestamp": now,
            }
        )
        with isolate_clv_db(), patch(
            "backend.models.sports.run_shadow_mlb.get_mlb_quant_probability",
            return_value={"home_prob": 0.65, "away_prob": 0.35},
        ), patch(
            "backend.models.sports.run_shadow_mlb._resolve_event_times",
            return_value=(now, future),
        ), patch(
            "backend.models.sports.run_shadow_mlb.sync_sports_market",
            side_effect=capture_sync,
        ):
            asyncio.run(
                run_mlb_moneyline_shadow(
                    router=router, db=MagicMock(), bankroll=1000.0, slate=slate
                )
            )
        self.assertEqual(captured["best_ask"], 0.44)
        self.assertEqual(captured["best_bid"], 0.41)
        self.assertAlmostEqual(captured["spread"], 0.03)
        self.assertEqual(captured["visible_depth"], 77.0)
        self.assertEqual(captured["outcome_id"], "tok_home")


if __name__ == "__main__":
    unittest.main()
