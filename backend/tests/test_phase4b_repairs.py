"""Phase 4b regression tests: game identity, CLV overdue, durable exposure, Poly ts."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.ml.mlb_quant_legacy import (
    MlbQuantGameIdentityAmbiguous,
    get_mlb_quant_probability,
    _select_schedule_game,
)
from backend.models.sports.run_shadow_mlb import (
    DUPLICATE_SHADOW_EXPOSURE,
    _dedupe_poly_markets,
    run_mlb_moneyline_shadow,
)
from backend.tests.clv_test_isolation import isolate_clv_db


def _sched_game(game_id, home, away, start_iso):
    return {
        "game_id": game_id,
        "home": {"name": home},
        "away": {"name": away},
        "game_time_et": start_iso,
        "home_pitcher": {"id": 1, "name": "A", "throws": "R", "era": 3.0},
        "away_pitcher": {"id": 2, "name": "B", "throws": "R", "era": 3.5},
        "venue_name": "Park",
    }


class TestMlbQuantGameIdentity(unittest.TestCase):
    def test_consecutive_same_teams_cannot_reuse_probability(self):
        """Jul 22 vs Jul 23 ATL@SD must resolve different gamePks → different probs."""
        g22 = _sched_game(
            716001,
            "Atlanta Braves",
            "San Diego Padres",
            "2026-07-22T19:10:00-04:00",
        )
        g23 = _sched_game(
            716002,
            "Atlanta Braves",
            "San Diego Padres",
            "2026-07-23T19:10:00-04:00",
        )

        def fake_todays(date_str=None):
            if date_str == "2026-07-22":
                return [g22]
            if date_str == "2026-07-23":
                return [g23]
            return []

        def fake_calc(game, bankroll):
            gid = game.get("game_id")
            # Distinct model outputs per identity (no coefficient change — stub only)
            home = 0.5694 if gid == 716001 else 0.4120
            return {"final_home_prob": home}

        with patch(
            "backend.ml.mlb_quant_legacy.get_todays_games", side_effect=fake_todays
        ), patch(
            "backend.ml.mlb_quant_legacy.calculate_win_probability",
            side_effect=fake_calc,
        ), patch(
            "backend.ml.mlb_quant_legacy.schedule_row_to_game",
            side_effect=lambda g: {
                "game_id": g["game_id"],
                "home_team": g["home"],
                "away_team": g["away"],
                "home_pitcher": g["home_pitcher"],
                "away_pitcher": g["away_pitcher"],
                "venue_name": g["venue_name"],
                "game_date": g["game_time_et"][:10],
                "is_dome": False,
            },
        ):
            p22 = get_mlb_quant_probability(
                "Atlanta Braves",
                "San Diego Padres",
                slate_date="2026-07-22",
                game_pk=716001,
            )
            p23 = get_mlb_quant_probability(
                "Atlanta Braves",
                "San Diego Padres",
                slate_date="2026-07-23",
                game_pk=716002,
            )

        self.assertIsNotNone(p22)
        self.assertIsNotNone(p23)
        self.assertAlmostEqual(p22["home_prob"], 0.5694)
        self.assertAlmostEqual(p23["home_prob"], 0.4120)
        self.assertNotEqual(p22["home_prob"], p23["home_prob"])
        self.assertEqual(p22["game_pk"], 716001)
        self.assertEqual(p23["game_pk"], 716002)

    def test_ambiguous_doubleheader_raises(self):
        g1 = _sched_game(
            1, "New York Yankees", "Boston Red Sox", "2026-07-22T13:05:00-04:00"
        )
        g2 = _sched_game(
            2, "New York Yankees", "Boston Red Sox", "2026-07-22T19:10:00-04:00"
        )
        with self.assertRaises(MlbQuantGameIdentityAmbiguous):
            _select_schedule_game(
                [g1, g2],
                "New York Yankees",
                "Boston Red Sox",
            )

    def test_exact_start_time_disambiguates_doubleheader(self):
        g1 = _sched_game(
            1, "New York Yankees", "Boston Red Sox", "2026-07-22T13:05:00-04:00"
        )
        g2 = _sched_game(
            2, "New York Yankees", "Boston Red Sox", "2026-07-22T19:10:00-04:00"
        )
        selected = _select_schedule_game(
            [g1, g2],
            "New York Yankees",
            "Boston Red Sox",
            scheduled_start_utc="2026-07-22T23:10:00+00:00",  # 19:10 ET
        )
        self.assertEqual(selected["game_id"], 2)


class TestDurableShadowExposure(unittest.TestCase):
    def test_stake_uses_effective_cost_times_shares(self):
        from backend.models.sports.run_shadow_mlb import obligation_stake_dollars

        row = {
            "entry_price": 0.50,  # market fill (understated if used alone)
            "entry_effective_cost": 0.5265,
            "metadata": {"shares": 40.0, "stake": 20.0},  # legacy market×shares
        }
        # Must prefer effective_cost × shares (= 21.06), not metadata.stake
        self.assertAlmostEqual(obligation_stake_dollars(row), 40.0 * 0.5265)

    def test_prior_2pct_outcome_blocks_second_fill(self):
        """A prior position at the 2% outcome cap must block another fill."""
        start = datetime.now(timezone.utc) + timedelta(hours=6)
        slate = [
            {
                "home_team": "Atlanta Braves",
                "away_team": "San Diego Padres",
                "slate_date": "2026-07-22",
                "scheduled_start_utc": start.isoformat(),
                "game_pk": 716001,
            }
        ]
        from backend.trading.market_matcher import _canonical

        event_id = (
            f"mlb_ml_2026-07-22_"
            f"{_canonical('Atlanta Braves') or 'Atlanta Braves'}_"
            f"{_canonical('San Diego Padres') or 'San Diego Padres'}"
        )
        bankroll = 1000.0
        # Exactly 2% outcome cap consumed via effective_cost × shares
        prior = MagicMock()
        prior.data = [
            {
                "candidate_id": f"sports_{event_id}_1",
                "outcome_id": "yes",
                "entry_price": 0.40,
                "entry_effective_cost": 0.50,
                "metadata": {"shares": 40.0},  # 40 * 0.50 = $20 = 2% of 1000
            }
        ]
        empty = MagicMock()
        empty.data = []

        db = MagicMock()
        # Run1: event lookup empty, outcome lookup empty
        # Run2: event lookup prior (20 < 50 event cap), outcome lookup prior → block
        like_chain = MagicMock()
        like_chain.execute.side_effect = [empty, empty, prior, prior]
        db.table.return_value.select.return_value.like.return_value = like_chain

        kalshi_mkt = SimpleNamespace(
            question="Will the Braves win?",
            market_id="KXMLBGAME-26JUL22ATLSD-ATL",
            slug="KXMLBGAME-26JUL22ATLSD-ATL",
            end_date="2026-07-22",
            venue="kalshi",
            yes_proposition_team="Atlanta Braves",
            outcomes=[
                SimpleNamespace(name="Yes", token_id="yes", price=0.40),
                SimpleNamespace(name="No", token_id="no", price=0.60),
            ],
        )
        book_ts = datetime.now(timezone.utc)
        router = MagicMock()
        router.kalshi.fetch_mlb_game_markets = AsyncMock(return_value=[kalshi_mkt])
        router.poly.fetch_markets = AsyncMock(return_value=[])
        router.get_top_of_book = AsyncMock(
            return_value={
                "best_ask": 0.40,
                "best_bid": 0.38,
                "ask_size": 500.0,
                "book_timestamp": book_ts,
                "received_timestamp": book_ts,
            }
        )
        sync_calls = []

        def fake_sync(**kwargs):
            sync_calls.append(kwargs)
            return {
                "would_trade": True,
                "paper_filled": True,
                "clv_obligation_created": True,
                "rejection_reason": None,
            }

        with isolate_clv_db(), patch(
            "backend.models.sports.run_shadow_mlb._moneyline_probs",
            return_value=(
                {"home_prob": 0.65, "away_prob": 0.35, "game_pk": 716001},
                {
                    "model_version": "t",
                    "feature_version": "t",
                    "coefficient_source": "t",
                    "calibration_status": "uncalibrated_shadow",
                },
            ),
        ), patch(
            "backend.models.sports.run_shadow_mlb.sync_sports_market",
            side_effect=fake_sync,
        ), patch(
            "backend.models.sports.run_shadow_mlb.estimate_fee_per_share",
            return_value=0.01,
        ):
            r1 = asyncio.run(
                run_mlb_moneyline_shadow(
                    router=router, db=db, bankroll=bankroll, slate=slate
                )
            )
            r2 = asyncio.run(
                run_mlb_moneyline_shadow(
                    router=router, db=db, bankroll=bankroll, slate=slate
                )
            )

        self.assertEqual(r1["processed"], 1)
        self.assertEqual(len(sync_calls), 1)
        # Second fill must hit outcome-cap duplicate, not invent another position
        self.assertEqual(r2["processed"], 0)
        self.assertIn(
            DUPLICATE_SHADOW_EXPOSURE,
            r2["by_venue"]["kalshi"]["rejection_reasons"],
        )
        # Caps passed into sync must shrink remaining outcome room on first fill
        caps = sync_calls[0]["risk_caps"]
        self.assertAlmostEqual(caps.max_outcome_exposure_pct, 0.02)

    def test_winning_book_refresh_zero_depth_rejects(self):
        from backend.models.sports.run_shadow_mlb import WINNING_BOOK_REFRESH_FAILED

        start = datetime.now(timezone.utc) + timedelta(hours=6)
        slate = [
            {
                "home_team": "Atlanta Braves",
                "away_team": "San Diego Padres",
                "slate_date": "2026-07-22",
                "scheduled_start_utc": start.isoformat(),
                "game_pk": 716001,
            }
        ]
        db = MagicMock()
        empty = MagicMock()
        empty.data = []
        db.table.return_value.select.return_value.like.return_value.execute.return_value = empty

        kalshi_mkt = SimpleNamespace(
            question="Will the Braves win?",
            market_id="KXMLBGAME-26JUL22ATLSD-ATL",
            slug="KXMLBGAME-26JUL22ATLSD-ATL",
            end_date="2026-07-22",
            venue="kalshi",
            yes_proposition_team="Atlanta Braves",
            outcomes=[
                SimpleNamespace(name="Yes", token_id="yes", price=0.40),
                SimpleNamespace(name="No", token_id="no", price=0.60),
            ],
        )
        book_ts = datetime.now(timezone.utc)
        good = {
            "best_ask": 0.40,
            "best_bid": 0.38,
            "ask_size": 500.0,
            "book_timestamp": book_ts,
            "received_timestamp": book_ts,
        }
        bad_refresh = {
            "best_ask": 0.40,
            "best_bid": 0.38,
            "ask_size": 0.0,  # zero refreshed depth → fail closed
            "book_timestamp": book_ts,
            "received_timestamp": book_ts,
        }
        router = MagicMock()
        router.kalshi.fetch_mlb_game_markets = AsyncMock(return_value=[kalshi_mkt])
        router.poly.fetch_markets = AsyncMock(return_value=[])
        # evaluate home + away, then refresh
        router.get_top_of_book = AsyncMock(side_effect=[good, good, bad_refresh])

        with isolate_clv_db(), patch(
            "backend.models.sports.run_shadow_mlb._moneyline_probs",
            return_value=(
                {"home_prob": 0.65, "away_prob": 0.35, "game_pk": 716001},
                {
                    "model_version": "t",
                    "feature_version": "t",
                    "coefficient_source": "t",
                    "calibration_status": "uncalibrated_shadow",
                },
            ),
        ), patch(
            "backend.models.sports.run_shadow_mlb.sync_sports_market",
        ) as sync_mock, patch(
            "backend.models.sports.run_shadow_mlb.estimate_fee_per_share",
            return_value=0.01,
        ):
            result = asyncio.run(
                run_mlb_moneyline_shadow(
                    router=router, db=db, bankroll=1000.0, slate=slate
                )
            )

        sync_mock.assert_not_called()
        self.assertEqual(result["processed"], 0)
        self.assertIn(
            WINNING_BOOK_REFRESH_FAILED,
            result["by_venue"]["kalshi"]["rejection_reasons"],
        )


class TestClvExclusionAndAnalyze(unittest.TestCase):
    def test_exclude_only_exact_contaminated_ids(self):
        from scripts.analyze_sports_shadow import (
            CLV_EVAL_EXCLUDE_CANDIDATE_IDS,
            _clv_excluded,
            summarize_durable_clv,
        )

        bad = (
            "sports_mlb_ml_2026-07-23_Atlanta Braves_San Diego Padres_1784721409"
        )
        good = (
            "sports_mlb_ml_2026-07-23_Cleveland Guardians_Minnesota Twins_1784731893"
        )
        self.assertIn(bad, CLV_EVAL_EXCLUDE_CANDIDATE_IDS)
        self.assertTrue(_clv_excluded(bad))
        self.assertFalse(_clv_excluded(good))
        summary = summarize_durable_clv(
            [
                {
                    "candidate_id": bad,
                    "status_15m": "unavailable",
                    "entry_price": 0.5,
                },
                {
                    "candidate_id": good,
                    "status_15m": "observed",
                    "obs_15m_price": 0.55,
                    "entry_market_price": 0.50,
                    "status_1h": "pending",
                    "status_close": "pending",
                },
            ]
        )
        self.assertEqual(summary["clv_records_excluded_reused_prob"], 1)
        self.assertEqual(summary["clv_records_evaluated"], 1)
        self.assertAlmostEqual(summary["average_clv_15m"], 0.05)


class TestPolymarketDedupeAndTimestamp(unittest.TestCase):
    def test_dedupe_by_market_id(self):
        a = SimpleNamespace(market_id="m1", slug="m1")
        b = SimpleNamespace(market_id="m1", slug="m1")
        c = SimpleNamespace(market_id="m2", slug="m2")
        out = _dedupe_poly_markets([a, b, c])
        self.assertEqual(len(out), 2)

    def test_polymarket_missing_book_ts_rejected(self):
        from backend.models.sports.run_shadow_mlb import _evaluate_venue_candidate, VenueStats

        start = datetime.now(timezone.utc) + timedelta(hours=3)
        market = SimpleNamespace(
            question="Braves vs Padres Winner 2026-07-22",
            market_id="pm1",
            slug="pm1",
            end_date="2026-07-22",
            venue="polymarket",
            outcomes=[
                SimpleNamespace(name="Atlanta Braves", token_id="tok_atl", price=0.5),
                SimpleNamespace(name="San Diego Padres", token_id="tok_sd", price=0.5),
            ],
            yes_proposition_team=None,
        )
        router = MagicMock()
        router.get_top_of_book = AsyncMock(
            return_value={
                "best_ask": 0.45,
                "best_bid": 0.44,
                "ask_size": 100.0,
                "book_timestamp": None,  # missing CLOB ts
                "received_timestamp": datetime.now(timezone.utc),
            }
        )
        stats = VenueStats()
        ev = asyncio.run(
            _evaluate_venue_candidate(
                router=router,
                venue="polymarket",
                markets=[market],
                home="Atlanta Braves",
                away="San Diego Padres",
                slate_date="2026-07-22",
                selected_team="Atlanta Braves",
                model_prob=0.60,
                start_time=start,
                stats=stats,
            )
        )
        self.assertFalse(ev["tradeable"])
        self.assertEqual(ev["rejection_reason"], "MISSING_ORDERBOOK_TIMESTAMP")


if __name__ == "__main__":
    unittest.main()
