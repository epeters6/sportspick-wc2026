import json
import os
import sys
from datetime import datetime, timezone, timedelta
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from backend.db import get_db
from backend.trading.venue_router import VenueRouter
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.risk_caps import RiskCaps
from backend.models.sports.sync_sports import sync_sports_market
from backend.trading.autobet import _current_bankroll
from backend.trading.market_matcher import _canonical, _parse_dt


def _lookup_match_start(db, team: str, opp: str, slate_date: str | None) -> datetime | None:
    """First pitch from the matches table when available."""
    team_c = _canonical(team) or team
    opp_c = _canonical(opp) or opp
    try:
        rows = (
            db.table("matches")
            .select("scheduled_at, home_team, away_team")
            .eq("sport", "mlb")
            .eq("is_final", False)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.debug(f"Match schedule lookup failed: {exc}")
        return None

    for row in rows:
        home = _canonical(row.get("home_team") or "") or row.get("home_team")
        away = _canonical(row.get("away_team") or "") or row.get("away_team")
        pair = {team_c, opp_c}
        if pair != {home, away}:
            continue
        start = _parse_dt(row.get("scheduled_at"))
        if start is None:
            continue
        if slate_date and start.strftime("%Y-%m-%d") != slate_date:
            continue
        return start
    return None


def _resolve_event_times(target_market, db, team: str, opp: str, slate_date: str | None) -> tuple[datetime, datetime]:
    """Return (snapshot_time, start_time) with snapshot strictly before first pitch."""
    snapshot = datetime.now(timezone.utc)
    start = _lookup_match_start(db, team, opp, slate_date)

    if start is None:
        end = _parse_dt(getattr(target_market, "end_date", None))
        if end and end > snapshot:
            # Polymarket close is usually after game end; back off ~3.5h for first pitch.
            start = end - timedelta(hours=3, minutes=30)

    if start is None or start <= snapshot:
        # Pregame fallback — keeps validate() happy when schedule metadata is missing.
        start = snapshot + timedelta(hours=3)

    return snapshot, start


def _quant_elo_diff(team: str, opp: str, data: dict) -> tuple[float, float, float]:
    """Map MLB quant win prob into Elo space for the logit model."""
    elo_a, elo_b, diff = 1500.0, 1500.0, 0.0
    try:
        from backend.ml.mlb_quant_legacy import get_mlb_quant_probability

        ctx = data.get("matchup_context") or {}
        venue_team = (data.get("team") or team).strip()
        home = venue_team if ctx.get("home_away") == "home" else opp
        away = opp if ctx.get("home_away") == "home" else venue_team
        quant = get_mlb_quant_probability(home, away)
        if not quant:
            return elo_a, elo_b, diff

        team_c = _canonical(team) or team
        home_c = _canonical(home) or home
        team_prob = float(quant["home_prob"] if team_c == home_c else quant["away_prob"])
        diff = round((team_prob - 0.5) * 800.0, 1)
        elo_a = 1500.0 + diff / 2.0
        elo_b = 1500.0 - diff / 2.0
    except Exception as exc:
        logger.debug(f"MLB quant bridge unavailable for {team} vs {opp}: {exc}")
    return elo_a, elo_b, diff


async def run_mlb_shadow_execution():
    logger.info("Running MLB shadow execution...")
    manifest = None
    try:
        from backend.ml.mlb_quant.orchestrator import load_existing_manifest

        manifest = load_existing_manifest()
    except Exception as exc:
        logger.warning(f"Could not load manifest from DB: {exc}")

    if not manifest:
        manifest_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "ml",
            "mlb_quant",
            "manifest.json",
        )
        if not os.path.exists(manifest_path):
            logger.warning(f"Manifest not found in DB or at {manifest_path}")
            return
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

    router = VenueRouter()
    db = get_db()
    bankroll = _current_bankroll(db)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    risk_caps = RiskCaps(
        max_event_exposure_pct=0.05,
        max_outcome_exposure_pct=0.02,
        max_strategy_exposure_pct=0.1,
        max_platform_exposure_pct=0.2,
        max_daily_loss_pct=0.05,
        max_weekly_loss_pct=0.1,
        min_net_edge=0.015,
        min_log_growth_delta=0.001,
    )

    processed = 0
    for p_key, data in manifest.items():
        if not isinstance(data, dict):
            continue
        if data.get("slate_date") and data.get("slate_date") != today_str:
            continue

        team = data.get("team")
        opp = data.get("opponent")
        date_str = data.get("slate_date") or today_str
        if not team or not opp:
            continue

        term = f"{team}"
        markets = await router.fetch_markets(search=term, limit=20)

        target_markets = []
        for m in markets:
            if team.lower() in m.question.lower() and opp.lower() in m.question.lower():
                target_markets.append(m)

        if len(target_markets) > 1:
            logger.warning(f"AMBIGUOUS_TEAM_MARKET_MATCH: Multiple markets found for {team} vs {opp}")
            continue
        if not target_markets:
            logger.info(f"Could not find Polymarket market for {team} vs {opp}")
            continue

        target_market = target_markets[0]

        if "winner" not in target_market.question.lower() and "moneyline" not in target_market.question.lower():
            logger.warning(f"UNSUPPORTED_MARKET_TYPE: Found market {target_market.question}")
            continue

        end_dt = _parse_dt(getattr(target_market, "end_date", None))
        if end_dt and end_dt < datetime.now(timezone.utc):
            logger.warning(f"INVALID_EVENT_TIME: Stale event for {team} vs {opp}")
            continue

        outcome = None
        for o in target_market.outcomes:
            if team.lower() in o.name.lower() or "yes" in o.name.lower():
                outcome = o
                break

        if not outcome:
            continue

        snapshot_time, start_time = _resolve_event_times(target_market, db, team, opp, date_str)
        elo_a, elo_b, elo_diff = _quant_elo_diff(team, opp, data)

        features = SportsEventFeatures(
            sport="MLB",
            league="mlb",
            event_id=f"mlb_{date_str}_{team}_{opp}".replace(" ", "_"),
            market_id=target_market.market_id,
            team_a=team,
            team_b=opp,
            start_time=start_time,
            snapshot_time=snapshot_time,
            market_prob_baseline=outcome.price,
            market_price_source="polymarket",
            elo_team_a=elo_a,
            elo_team_b=elo_b,
            elo_diff=elo_diff,
            consensus_pick_count_a=0,
            consensus_pick_count_b=0,
            consensus_weighted_signal=0.0,
            source_clv_weighted_signal=0.0,
            source_count=0,
            independent_source_count=0,
            sport_specific={
                "pitcher": data.get("name"),
                "tier": data.get("tier"),
                "prop_line": data.get("prop_line"),
                "manager_hook_score": data.get("advanced_context", {}).get("manager_hook_score", 0),
                "quant_elo_diff": elo_diff,
            },
        )

        best_ask = outcome.best_ask or outcome.price
        visible_depth = target_market.liquidity

        real_received = getattr(target_market, "received_timestamp", datetime.now(timezone.utc))
        real_exch = getattr(target_market, "exchange_timestamp", datetime.now(timezone.utc))

        sync_sports_market(
            market_data={"platform": target_market.venue},
            features=features,
            best_ask=best_ask,
            fee_per_share=0.01,
            visible_depth=visible_depth,
            bankroll=bankroll,
            risk_caps=risk_caps,
            mode="shadow",
            real_orderbook_timestamp=real_exch,
            real_received_timestamp=real_received,
        )
        processed += 1

    logger.info(f"MLB shadow execution complete. Processed {processed} markets.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_mlb_shadow_execution())
