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
from backend.models.sports.mlb_contract_match import match_pitcher_outs_contract
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


def _resolve_event_times(
    target_market,
    db,
    team: str,
    opp: str,
    slate_date: str | None,
    scheduled_start_utc: str | None = None,
) -> tuple[datetime, datetime] | None:
    """Return (snapshot_time, start_time). Refuse post-start / invented times."""
    snapshot = datetime.now(timezone.utc)
    start = _parse_dt(scheduled_start_utc) if scheduled_start_utc else None
    if start is None:
        start = _lookup_match_start(db, team, opp, slate_date)

    if start is None:
        logger.info(f"NO_SCHEDULED_START: {team} vs {opp} — skipping (will not invent timestamp)")
        return None

    if start.tzinfo is None:
        logger.info(f"NAIVE_EVENT_START: {team} vs {opp}")
        return None

    if start <= snapshot:
        logger.info(f"POST_START_OR_LIVE: {team} vs {opp} start={start.isoformat()}")
        return None

    return snapshot, start


def _pitcher_outs_prob(data: dict, side: str) -> tuple[float, dict]:
    """
    Use pitcher-outs directional model when available; do not silently swap to moneyline Elo.
    Coefficients/weights untouched — only wiring.
    """
    meta = {
        "model_version": data.get("model_version") or "mlb_pitcher_outs_v4",
        "feature_version": data.get("feature_version") or "mlb_quant_manifest_v1",
        "coefficient_source": data.get("coefficient_source") or "under_model_state.json",
        "calibration_status": data.get("calibration_status") or "uncalibrated_shadow",
    }
    try:
        from backend.ml.mlb_quant.mlb_pitcher_fatigue_engine_v4 import (
            load_model_state,
            build_feature_vector,
        )
    except Exception as exc:
        logger.debug(f"Pitcher outs engine unavailable: {exc}")
        return 0.0, {**meta, "rejection": "PITCHER_OUTS_MODEL_UNAVAILABLE"}

    # Prefer any prediction already attached to manifest
    pred = data.get("prediction") or {}
    if isinstance(pred, dict):
        under_p = pred.get("under_proba")
        over_p = pred.get("over_proba")
        if under_p is not None and over_p is not None:
            p = float(under_p if side == "UNDER" else over_p)
            return p, meta

    # Fall back: baseline expected outs vs line as a simple logistic proxy WITHOUT
    # changing stored coefficients — uses expected_outs_baseline from profile only.
    try:
        profile = data.get("starter_profile") or data.get("advanced_context") or {}
        expected = float(
            profile.get("expected_outs_baseline")
            or (data.get("baseline") or {}).get("expected_outs_baseline")
            or 15.0
        )
        line = float(data.get("prop_line") or 17.5)
        # Soft distance → probability; complementary for OVER/UNDER
        import math

        gap = expected - line
        # Positive gap => more outs than line => OVER more likely
        over_p = 1.0 / (1.0 + math.exp(-0.35 * gap))
        under_p = 1.0 - over_p
        p = under_p if side == "UNDER" else over_p
        return float(p), {**meta, "prob_method": "expected_outs_vs_line_logit"}
    except Exception as exc:
        logger.debug(f"Pitcher outs prob fallback failed: {exc}")
        return 0.0, {**meta, "rejection": "PITCHER_OUTS_PROB_FAILED"}


async def run_mlb_shadow_execution():
    logger.info("Running MLB shadow execution (pitcher_outs contracts only)...")
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
            raise RuntimeError(
                "MLB_MANIFEST_UNAVAILABLE: no manifest in DB or on disk — "
                "refusing zero-valued validation success"
            )
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    if not manifest:
        raise RuntimeError("MLB_MANIFEST_EMPTY")

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
    rejected = 0
    for p_key, data in manifest.items():
        if not isinstance(data, dict):
            continue
        if data.get("slate_date") and data.get("slate_date") != today_str:
            continue

        contract_type = data.get("contract_type") or "pitcher_outs"
        if contract_type != "pitcher_outs":
            # Moneyline (or other) paths must be separate manifests — do not mix.
            logger.info(f"SKIP_NON_PITCHER_OUTS_CONTRACT: {p_key} type={contract_type}")
            rejected += 1
            continue

        team = data.get("team")
        opp = data.get("opponent")
        pitcher = data.get("name")
        date_str = data.get("slate_date") or today_str
        prop_line = float(data.get("prop_line") or 17.5)
        prop_side = (data.get("prop_side") or "UNDER").upper()
        if not team or not opp or not pitcher:
            rejected += 1
            continue

        # Search by pitcher name — not raw team abbreviation substring as primary key
        markets = await router.fetch_markets(search=str(pitcher), limit=30)
        if not markets:
            markets = await router.fetch_markets(search=f"{pitcher} outs", limit=30)

        matched = match_pitcher_outs_contract(
            markets=markets or [],
            pitcher_name=str(pitcher),
            team=str(team),
            opponent=str(opp),
            slate_date=date_str,
            prop_line=prop_line,
            prop_side=prop_side,
        )
        if matched.rejection_reason:
            logger.info(
                f"{matched.rejection_reason}: pitcher={pitcher} line={prop_line} side={prop_side}"
            )
            rejected += 1
            continue

        target_market = matched.market
        outcome = matched.outcome
        side = matched.side

        ctx = data.get("matchup_context") or {}
        times = _resolve_event_times(
            target_market,
            db,
            team,
            opp,
            date_str,
            scheduled_start_utc=ctx.get("scheduled_start_utc"),
        )
        if times is None:
            rejected += 1
            continue
        snapshot_time, start_time = times

        model_prob, model_meta = _pitcher_outs_prob(data, side)
        if model_prob <= 0 or model_meta.get("rejection"):
            logger.info(
                f"{model_meta.get('rejection') or 'ZERO_MODEL_PROB'}: {pitcher}"
            )
            rejected += 1
            continue

        # Encode pitcher-outs features into SportsEventFeatures.sport_specific
        # so they reach the probability path / audit trail (coefficients unchanged).
        features = SportsEventFeatures(
            sport="MLB",
            league="mlb",
            event_id=f"mlb_outs_{date_str}_{pitcher}_{prop_line}_{side}".replace(" ", "_"),
            market_id=target_market.market_id,
            team_a=str(pitcher),
            team_b=f"{side}_{prop_line}",
            start_time=start_time,
            snapshot_time=snapshot_time,
            market_prob_baseline=outcome.price,
            market_price_source="polymarket",
            elo_team_a=1500.0,
            elo_team_b=1500.0,
            elo_diff=0.0,
            consensus_pick_count_a=0,
            consensus_pick_count_b=0,
            consensus_weighted_signal=0.0,
            source_clv_weighted_signal=0.0,
            source_count=0,
            independent_source_count=0,
            sport_specific={
                "contract_type": "pitcher_outs",
                "pitcher": pitcher,
                "pitcher_id": data.get("pitcher_id"),
                "team": team,
                "opponent": opp,
                "tier": data.get("tier"),
                "prop_line": prop_line,
                "prop_side": side,
                "model_prob_override": model_prob,
                "manager_hook_score": (data.get("advanced_context") or {}).get(
                    "manager_hook_score", 0
                ),
                **model_meta,
            },
        )

        best_ask = outcome.best_ask or outcome.price
        visible_depth = getattr(outcome, "ask_size", None) or getattr(
            target_market, "liquidity", None
        )
        if visible_depth is None or float(visible_depth) <= 0:
            logger.info(f"INSUFFICIENT_DEPTH: {pitcher} market={target_market.market_id}")
            rejected += 1
            continue

        real_received = getattr(target_market, "received_timestamp", None)
        real_exch = getattr(target_market, "exchange_timestamp", None) or getattr(
            target_market, "orderbook_timestamp", None
        )
        # Never invent timestamps for evidence fills
        if real_received is None or real_exch is None:
            logger.info(
                f"MISSING_ORDERBOOK_TIMESTAMP: {pitcher} market={target_market.market_id}"
            )
            rejected += 1
            continue

        sync_sports_market(
            market_data={
                "platform": target_market.venue,
                "contract_type": "pitcher_outs",
                "model_prob_override": model_prob,
            },
            features=features,
            best_ask=best_ask,
            fee_per_share=0.01,
            visible_depth=float(visible_depth),
            bankroll=bankroll,
            risk_caps=risk_caps,
            mode="shadow",
            real_orderbook_timestamp=real_exch,
            real_received_timestamp=real_received,
        )
        processed += 1

    logger.info(
        f"MLB shadow execution complete. processed={processed} rejected={rejected}"
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_mlb_shadow_execution())
