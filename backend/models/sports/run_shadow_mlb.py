"""
MLB shadow execution — moneyline (primary) + optional pitcher-outs.

Moneyline uses ``backend.ml.mlb_quant_legacy.get_mlb_quant_probability``.
Pitcher-outs remains an optional separate strategy. When its pregame model is
unavailable it reports ``PREGAME_MODEL_UNAVAILABLE`` only in that report field
and must not fail the moneyline validation job.

Live submission remains disabled (``mode="shadow"`` only).
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
)

from backend.db import get_db
from backend.trading.venue_router import VenueRouter
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.risk_caps import RiskCaps
from pavlov.pipeline.fee_model import estimate_fee_per_share
from backend.models.sports.sync_sports import sync_sports_market
from backend.models.sports.mlb_contract_match import match_pitcher_outs_contract
from backend.models.sports.mlb_moneyline_match import match_mlb_moneyline_contract
from backend.trading.autobet import _current_bankroll
from backend.trading.market_matcher import _canonical, _parse_dt
from backend.ml.mlb_quant_legacy import get_mlb_quant_probability

PREGAME_MODEL_UNAVAILABLE = "PREGAME_MODEL_UNAVAILABLE"
MLB_SHADOW_ZERO_PROCESSED = "MLB_SHADOW_ZERO_PROCESSED"
MLB_MONEYLINE_MANIFEST_EMPTY = "MLB_MONEYLINE_MANIFEST_EMPTY"

COEFFICIENT_SOURCE = "mlb_quant_legacy.calculate_win_probability"
MODEL_VERSION = "mlb_quant_legacy"
FEATURE_VERSION = "mlb_moneyline_v1"
MODEL_TYPE = "mlb_quant_legacy"
NY_TZ = ZoneInfo("America/New_York")
ENABLE_PITCHER_OUTS = os.environ.get("MLB_SHADOW_PITCHER_OUTS", "0") == "1"


def _mlb_game_date(start: datetime) -> str:
    """Official MLB calendar date in America/New_York (not UTC date)."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start.astimezone(NY_TZ).strftime("%Y-%m-%d")


@dataclass
class VenueStats:
    discovered: int = 0
    matched: int = 0
    rejected: int = 0
    would_trade: int = 0
    paper_filled: int = 0
    effective_costs: list = field(default_factory=list)
    net_edges: list = field(default_factory=list)
    clv_obligations: int = 0
    rejection_reasons: dict = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "matched": self.matched,
            "rejected": self.rejected,
            "would_trade": self.would_trade,
            "paper_filled": self.paper_filled,
            "effective_cost": (
                sum(self.effective_costs) / len(self.effective_costs)
                if self.effective_costs
                else None
            ),
            "net_edge": (
                sum(self.net_edges) / len(self.net_edges) if self.net_edges else None
            ),
            "CLV_obligation_created": self.clv_obligations,
            "rejection_reasons": dict(self.rejection_reasons),
        }


def _lookup_match_start(db, team: str, opp: str, slate_date: str | None) -> datetime | None:
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
        if {team_c, opp_c} != {home, away}:
            continue
        start = _parse_dt(row.get("scheduled_at"))
        if start is None:
            continue
        if slate_date and start.strftime("%Y-%m-%d") != slate_date:
            continue
        return start
    return None


def _resolve_event_times(
    db,
    home: str,
    away: str,
    slate_date: str | None,
    scheduled_start_utc: str | None = None,
) -> tuple[datetime, datetime] | None:
    snapshot = datetime.now(timezone.utc)
    start = _parse_dt(scheduled_start_utc) if scheduled_start_utc else None
    if start is None:
        start = _lookup_match_start(db, home, away, slate_date)
    if start is None:
        logger.info(f"NO_SCHEDULED_START: {home} vs {away}")
        return None
    if start.tzinfo is None:
        logger.info(f"NAIVE_EVENT_START: {home} vs {away}")
        return None
    if start <= snapshot:
        logger.info(f"POST_START_OR_LIVE: {home} vs {away} start={start.isoformat()}")
        return None
    return snapshot, start


def _valid_prob(p: Any) -> bool:
    try:
        v = float(p)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and 0.0 < v < 1.0


def _moneyline_probs(home: str, away: str) -> tuple[dict | None, dict]:
    """Fetch home/away win probs once per game."""
    meta = {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "coefficient_source": COEFFICIENT_SOURCE,
        "calibration_status": "uncalibrated_shadow",
        "market_type": "moneyline",
        "strategy": "mlb_moneyline",
        "model_type": MODEL_TYPE,
    }
    probs = get_mlb_quant_probability(home, away)
    if not probs:
        return None, {**meta, "rejection": "MLB_QUANT_PROB_UNAVAILABLE"}
    home_p = probs.get("home_prob")
    away_p = probs.get("away_prob")
    if not _valid_prob(home_p) or not _valid_prob(away_p):
        return None, {**meta, "rejection": "MLB_QUANT_PROB_INVALID"}
    return {
        "home_prob": float(home_p),
        "away_prob": float(away_p),
    }, meta


def _prob_for_team(probs: dict, home: str, away: str, selected: str) -> float:
    home_c = _canonical(home) or home
    sel_c = _canonical(selected) or selected
    if sel_c == home_c or selected == home:
        return float(probs["home_prob"])
    return float(probs["away_prob"])


def _load_moneyline_slate(db) -> list[dict]:
    """Nonempty slate of upcoming MLB games (home/away/NY-date/start)."""
    slate: list[dict] = []
    now = datetime.now(timezone.utc)
    try:
        rows = (
            db.table("matches")
            .select("id, home_team, away_team, scheduled_at, sport, is_final")
            .eq("sport", "mlb")
            .eq("is_final", False)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning(f"Moneyline slate DB load failed: {exc}")
        rows = []

    for row in rows:
        home = row.get("home_team")
        away = row.get("away_team")
        start = _parse_dt(row.get("scheduled_at"))
        if not home or not away or start is None or start.tzinfo is None:
            continue
        if start <= now:
            continue
        slate.append(
            {
                "home_team": home,
                "away_team": away,
                "slate_date": _mlb_game_date(start),
                "scheduled_start_utc": start.isoformat(),
                "match_id": row.get("id"),
            }
        )

    if slate:
        return slate

    try:
        from pipeline.mlb_client import get_todays_games

        for g in get_todays_games() or []:
            home = (g.get("home") or {}).get("name")
            away = (g.get("away") or {}).get("name")
            if not home or not away:
                continue
            start_s = g.get("game_datetime") or g.get("gameDate")
            start = _parse_dt(start_s)
            if start is None or start.tzinfo is None or start <= now:
                continue
            official = g.get("game_date") or g.get("official_date") or _mlb_game_date(start)
            slate.append(
                {
                    "home_team": home,
                    "away_team": away,
                    "slate_date": str(official)[:10],
                    "scheduled_start_utc": start.isoformat(),
                    "match_id": g.get("game_pk"),
                }
            )
    except Exception as exc:
        logger.warning(f"Moneyline schedule fallback failed: {exc}")

    return slate


def _executable_cost(platform: str, best_ask: float) -> float:
    fee = estimate_fee_per_share(platform, float(best_ask), 1.0)
    return float(best_ask) + float(fee) + 0.005


def _pitcher_outs_prob(data: dict, side: str) -> tuple[float, dict]:
    """Optional pitcher-outs path — never invent pregame probs."""
    meta = {
        "model_version": data.get("model_version") or "mlb_pitcher_outs_v4",
        "feature_version": data.get("feature_version") or "mlb_quant_manifest_v1",
        "coefficient_source": data.get("coefficient_source") or "under_model_state.json",
        "calibration_status": data.get("calibration_status") or "uncalibrated_shadow",
    }
    pred = data.get("prediction")
    if isinstance(pred, dict):
        under_p = pred.get("under_proba")
        over_p = pred.get("over_proba")
        if under_p is not None and over_p is not None:
            p = float(under_p if side == "UNDER" else over_p)
            if p <= 0.0 or p >= 1.0:
                return 0.0, {**meta, "rejection": "PITCHER_OUTS_PRED_OUT_OF_RANGE"}
            return p, {**meta, "prob_method": "in_game_fatigue_prediction"}
    return 0.0, {
        **meta,
        "rejection": PREGAME_MODEL_UNAVAILABLE,
        "note": (
            "Selected path is a pregame pitcher-outs model (not yet implemented). "
            "In-game fatigue is a separate workflow — do not invent pregame probs."
        ),
    }


def report_pitcher_outs_availability(manifest: dict | None = None) -> dict:
    """Separate pitcher-outs status — does not raise or block moneyline."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if manifest is None:
        try:
            from backend.ml.mlb_quant.orchestrator import load_existing_manifest

            manifest = load_existing_manifest() or {}
        except Exception:
            manifest = {}
    has_in_game = False
    for data in (manifest or {}).values():
        if not isinstance(data, dict):
            continue
        if data.get("slate_date") and data.get("slate_date") != today:
            continue
        pred = data.get("prediction")
        if (
            isinstance(pred, dict)
            and pred.get("under_proba") is not None
            and pred.get("over_proba") is not None
        ):
            has_in_game = True
            break
    if has_in_game:
        status = "in_game_fatigue_available"
        rejection = None
    else:
        status = PREGAME_MODEL_UNAVAILABLE
        rejection = PREGAME_MODEL_UNAVAILABLE
    return {
        "strategy": "mlb_pitcher_outs",
        "enabled": ENABLE_PITCHER_OUTS,
        "availability": status,
        "rejection": rejection,
    }


async def _evaluate_venue_candidate(
    *,
    router: VenueRouter,
    venue: str,
    markets: list,
    home: str,
    away: str,
    slate_date: str,
    selected_team: str,
    model_prob: float,
    start_time: datetime,
    stats: VenueStats,
) -> dict | None:
    matched = match_mlb_moneyline_contract(
        markets=markets,
        home_team=home,
        away_team=away,
        slate_date=slate_date,
        selected_team=selected_team,
        venue=venue,
    )
    if matched.rejection_reason:
        stats.reject(matched.rejection_reason)
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": matched.rejection_reason,
            "selected_team": selected_team,
        }
    token_id = getattr(matched.outcome, "token_id", None)
    if not token_id:
        stats.reject("MISSING_OUTCOME_TOKEN_ID")
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": "MISSING_OUTCOME_TOKEN_ID",
            "selected_team": selected_team,
        }
    stats.matched += 1
    market = matched.market
    book = await router.get_top_of_book(
        venue=venue,
        token_id=str(token_id),
        market_id=market.market_id,
    )
    best_ask = book.get("best_ask")
    best_bid = book.get("best_bid")
    ask_size = float(book.get("ask_size") or 0.0)
    book_ts = book.get("book_timestamp")
    received_ts = book.get("received_timestamp")
    if received_ts is None:
        stats.reject("MISSING_RECEIVED_TIMESTAMP")
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": "MISSING_RECEIVED_TIMESTAMP",
            "selected_team": selected_team,
        }
    if received_ts.tzinfo is None:
        stats.reject("NAIVE_RECEIVED_TIMESTAMP")
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": "NAIVE_RECEIVED_TIMESTAMP",
            "selected_team": selected_team,
        }
    if received_ts >= start_time:
        stats.reject("BOOK_RECEIPT_AFTER_FIRST_PITCH")
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": "BOOK_RECEIPT_AFTER_FIRST_PITCH",
            "selected_team": selected_team,
        }

    if best_ask is None or best_bid is None:
        stats.reject("MISSING_TOP_OF_BOOK")
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": "MISSING_TOP_OF_BOOK",
            "selected_team": selected_team,
        }
    if ask_size <= 0:
        stats.reject("INSUFFICIENT_DEPTH")
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": "INSUFFICIENT_DEPTH",
            "selected_team": selected_team,
        }
    if book_ts is None and venue != "kalshi":
        # Polymarket may fall back to received_timestamp for freshness in shadow
        # only when explicitly allowed; require exchange book time when present path
        # expects it. Shadow polymarket still needs a book_ts OR we allow received.
        pass
    if book_ts is None and venue == "polymarket":
        # Allow shadow freshness from receipt only if exchange stamp missing —
        # still do not invent an exchange timestamp.
        pass

    try:
        eff = _executable_cost(venue, float(best_ask))
    except ValueError as exc:
        stats.reject(str(exc))
        return {
            "venue": venue,
            "tradeable": False,
            "rejection_reason": str(exc),
            "selected_team": selected_team,
        }

    net_edge = float(model_prob) - eff
    stats.effective_costs.append(eff)
    stats.net_edges.append(net_edge)
    return {
        "venue": venue,
        "tradeable": True,
        "market": market,
        "outcome": matched.outcome,
        "side": matched.side,
        "token_id": str(token_id),
        "yes_team": matched.yes_team,
        "best_ask": float(best_ask),
        "best_bid": float(best_bid),
        "spread": float(best_ask) - float(best_bid),
        "ask_size": ask_size,
        "book_ts": book_ts,
        "received_ts": received_ts,
        "effective_cost": eff,
        "net_edge": net_edge,
        "model_prob": model_prob,
        "selected_team": selected_team,
        "missing_orderbook_timestamp": book_ts is None,
        "timestamp_source": (
            book.get("timestamp_source")
            if book_ts is None
            else "orderbook_timestamp"
        ),
        "allow_received_timestamp_shadow": book_ts is None,
    }


def _select_best_candidate(tradeable: list[dict]) -> dict | None:
    """
    Per team → best venue (lowest effective cost).
    Across teams → highest positive net edge.
    """
    by_team: dict[str, dict] = {}
    for c in tradeable:
        team = c["selected_team"]
        prev = by_team.get(team)
        if prev is None or c["effective_cost"] < prev["effective_cost"]:
            by_team[team] = c
    positive = [c for c in by_team.values() if c.get("net_edge", 0) > 0]
    if not positive:
        return None
    return max(positive, key=lambda c: c["net_edge"])


async def run_mlb_moneyline_shadow(
    *,
    router: VenueRouter | None = None,
    db=None,
    bankroll: float | None = None,
    slate: list[dict] | None = None,
) -> dict:
    """Primary MLB shadow path: home/away win probability → moneyline markets."""
    router = router or VenueRouter()
    db = db if db is not None else get_db()
    bankroll = float(bankroll if bankroll is not None else _current_bankroll(db))
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

    if slate is None:
        slate = _load_moneyline_slate(db)
    if not slate:
        raise RuntimeError(
            f"{MLB_MONEYLINE_MANIFEST_EMPTY}: no upcoming MLB games for moneyline shadow"
        )

    poly_stats = VenueStats()
    kalshi_stats = VenueStats()
    processed = 0
    rejected = 0
    exposed_events: set[str] = set()
    candidate_evaluations = 0

    # Fetch Kalshi MLB slate once per run
    try:
        kalshi_slate = await router.kalshi.fetch_mlb_game_markets(limit=200)
    except Exception as exc:
        logger.warning(f"Kalshi MLB slate fetch failed: {exc}")
        kalshi_slate = []
    kalshi_stats.discovered = len(kalshi_slate)

    for game in slate:
        home = game["home_team"]
        away = game["away_team"]
        slate_date = game["slate_date"]
        times = _resolve_event_times(
            db, home, away, slate_date, game.get("scheduled_start_utc")
        )
        if times is None:
            rejected += 1
            continue
        _pre_discovery_snapshot, start_time = times
        event_id = (
            f"mlb_ml_{slate_date}_{_canonical(home) or home}_{_canonical(away) or away}"
        )
        if event_id in exposed_events:
            continue

        # Polymarket discovery per game; Kalshi from once-per-run slate
        markets: list = list(kalshi_slate)
        search = f"{home} vs {away}"
        try:
            poly = await router.poly.fetch_markets(search=search, limit=80)
            for m in poly or []:
                m.venue = "polymarket"
                markets.append(m)
        except Exception as exc:
            logger.warning(f"Polymarket discovery failed for {search}: {exc}")
        for term in (home, away, f"{away} @ {home}"):
            try:
                extra = await router.poly.fetch_markets(search=term, limit=40)
                for m in extra or []:
                    m.venue = "polymarket"
                    markets.append(m)
            except Exception:
                pass

        poly_n = len(
            [m for m in markets if (getattr(m, "venue", "") or "").lower() == "polymarket"]
        )
        poly_stats.discovered += poly_n

        probs, meta = _moneyline_probs(home, away)
        if probs is None:
            reason = meta.get("rejection") or "MLB_QUANT_PROB_UNAVAILABLE"
            logger.info(f"{reason}: {home} vs {away}")
            poly_stats.reject(reason)
            kalshi_stats.reject(reason)
            rejected += 1
            continue

        venue_candidates: list[dict] = []
        for selected in (home, away):
            model_prob = _prob_for_team(probs, home, away, selected)
            for venue, stats in (("polymarket", poly_stats), ("kalshi", kalshi_stats)):
                candidate_evaluations += 1
                ev = await _evaluate_venue_candidate(
                    router=router,
                    venue=venue,
                    markets=markets,
                    home=home,
                    away=away,
                    slate_date=slate_date,
                    selected_team=selected,
                    model_prob=model_prob,
                    start_time=start_time,
                    stats=stats,
                )
                if ev:
                    ev["model_meta"] = meta
                    venue_candidates.append(ev)

        tradeable = [c for c in venue_candidates if c.get("tradeable")]
        if not tradeable:
            for c in venue_candidates:
                logger.info(
                    f"moneyline reject venue={c.get('venue')} team={c.get('selected_team')} "
                    f"reason={c.get('rejection_reason')}"
                )
            rejected += 1
            continue

        best = _select_best_candidate(tradeable)
        if best is None:
            logger.info(
                f"NO_POSITIVE_NET_EDGE: {home} vs {away}; "
                f"candidates={[ (c['venue'], c['selected_team'], round(c['net_edge'], 4)) for c in tradeable ]}"
            )
            rejected += 1
            exposed_events.add(event_id)
            continue

        for c in tradeable:
            if c is best:
                continue
            logger.info(
                f"CANDIDATE_NOT_SELECTED: venue={c['venue']} team={c['selected_team']} "
                f"net_edge={c['net_edge']:.4f} eff={c['effective_cost']:.4f} "
                f"best_venue={best['venue']} best_team={best['selected_team']} "
                f"best_net_edge={best['net_edge']:.4f}"
            )

        meta = best["model_meta"]
        venue = best["venue"]
        stats = poly_stats if venue == "polymarket" else kalshi_stats
        fee = estimate_fee_per_share(venue, best["best_ask"], 1.0)
        # Snapshot = selected book receipt (not pre-discovery wall clock)
        snapshot_time = best["received_ts"]
        features = SportsEventFeatures(
            sport="mlb",
            league="mlb",
            event_id=event_id,
            market_id=best["market"].market_id,
            team_a=home,
            team_b=away,
            start_time=start_time,
            snapshot_time=snapshot_time,
            market_prob_baseline=best["best_ask"],
            market_price_source=f"{venue}_top_of_book",
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
                "contract_type": "moneyline",
                "market_type": "moneyline",
                "strategy": "mlb_moneyline",
                "model_type": MODEL_TYPE,
                "selected_team": best["selected_team"],
                "contract_side": best["side"],
                "model_prob_override": best["model_prob"],
                "outcome_token_id": best["token_id"],
                "yes_proposition_team": best.get("yes_team"),
                **{
                    k: meta[k]
                    for k in (
                        "model_version",
                        "feature_version",
                        "coefficient_source",
                        "calibration_status",
                    )
                },
            },
        )

        sync_result = sync_sports_market(
            market_data={
                "platform": venue,
                "contract_type": "moneyline",
                "model_prob_override": best["model_prob"],
                "outcome_id": best["token_id"],
                "kalshi_moneyline_mapping_verified": venue == "kalshi",
                "allow_received_timestamp_shadow": bool(
                    best.get("allow_received_timestamp_shadow")
                )
                or venue == "kalshi",
                "timestamp_source": best.get("timestamp_source"),
            },
            features=features,
            best_ask=best["best_ask"],
            best_bid=best["best_bid"],
            spread=best["spread"],
            fee_per_share=fee,
            visible_depth=best["ask_size"],
            bankroll=bankroll,
            risk_caps=risk_caps,
            mode="shadow",
            real_orderbook_timestamp=best["book_ts"],
            real_received_timestamp=best["received_ts"],
            outcome_id=best["token_id"],
        )

        exposed_events.add(event_id)
        if sync_result.get("would_trade"):
            stats.would_trade += 1
        if sync_result.get("paper_filled"):
            stats.paper_filled += 1
            processed += 1
        if sync_result.get("clv_obligation_created"):
            stats.clv_obligations += 1
        if sync_result.get("rejection_reason"):
            rejected += 1
            stats.reject(str(sync_result["rejection_reason"]))

    return {
        "strategy": "mlb_moneyline",
        "slate_size": len(slate),
        "processed": processed,
        "rejected": rejected,
        "candidate_evaluations": candidate_evaluations,
        "exposed_events": len(exposed_events),
        "by_venue": {
            "polymarket": poly_stats.as_dict(),
            "kalshi": kalshi_stats.as_dict(),
        },
    }


async def run_mlb_shadow_execution():
    """Moneyline shadow validation entrypoint (pitcher-outs optional/non-blocking)."""
    logger.info("Running MLB shadow execution (moneyline primary)...")
    pitcher_report = report_pitcher_outs_availability()
    logger.info(f"Pitcher-outs report: {pitcher_report}")

    moneyline_report = await run_mlb_moneyline_shadow()
    logger.info(f"Moneyline report: {moneyline_report}")

    # Persist combined report artifact for the validation runner
    report = {
        "moneyline": moneyline_report,
        "pitcher_outs": pitcher_report,
        "mode": "shadow",
        "live_disabled": True,
    }
    try:
        os.makedirs("reports/sports_shadow", exist_ok=True)
        path = os.path.join(
            "reports",
            "sports_shadow",
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}_mlb_shadow_status.json",
        )
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
    except OSError as exc:
        logger.warning(f"Could not write mlb shadow status: {exc}")

    # Zero processed only when moneyline path had slate work but no valid processed fills
    # after market/book rejection — edge-only rejects still mark exposed_events.
    if (
        moneyline_report["slate_size"] > 0
        and moneyline_report["exposed_events"] == 0
        and moneyline_report["candidate_evaluations"] > 0
    ):
        raise RuntimeError(
            f"{MLB_SHADOW_ZERO_PROCESSED}: moneyline path produced zero valid "
            f"processed candidates after market/book rejection; "
            f"report={moneyline_report['by_venue']}"
        )

    # Optional pitcher-outs execution (does not fail moneyline job on PREGAME)
    if ENABLE_PITCHER_OUTS:
        try:
            await _run_optional_pitcher_outs_shadow()
        except Exception as exc:
            logger.warning(f"Optional pitcher-outs shadow failed (non-blocking): {exc}")

    logger.info("MLB shadow execution complete.")
    return report


async def _run_optional_pitcher_outs_shadow() -> None:
    """Optional pitcher-outs loop — kept for future pregame model wiring."""
    from backend.ml.mlb_quant.orchestrator import load_existing_manifest

    manifest = load_existing_manifest() or {}
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    router = VenueRouter()
    db = get_db()
    bankroll = _current_bankroll(db)
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
    for _p_key, data in manifest.items():
        if not isinstance(data, dict):
            continue
        if data.get("slate_date") and data.get("slate_date") != today_str:
            continue
        if (data.get("contract_type") or "pitcher_outs") != "pitcher_outs":
            continue
        team = data.get("team")
        opp = data.get("opponent")
        pitcher = data.get("name")
        if not team or not opp or not pitcher:
            continue
        model_prob, model_meta = _pitcher_outs_prob(data, (data.get("prop_side") or "UNDER").upper())
        if model_meta.get("rejection") == PREGAME_MODEL_UNAVAILABLE:
            logger.info(f"{PREGAME_MODEL_UNAVAILABLE}: {pitcher}")
            continue
        if model_prob <= 0 or model_meta.get("rejection"):
            continue
        markets = await router.fetch_markets(search=str(pitcher), limit=30)
        matched = match_pitcher_outs_contract(
            markets=markets or [],
            pitcher_name=str(pitcher),
            team=str(team),
            opponent=str(opp),
            slate_date=data.get("slate_date") or today_str,
            prop_line=float(data.get("prop_line") or 17.5),
            prop_side=(data.get("prop_side") or "UNDER").upper(),
        )
        if matched.rejection_reason or not matched.outcome:
            continue
        token_id = getattr(matched.outcome, "token_id", None)
        if not token_id:
            continue
        times = _resolve_event_times(
            db,
            team,
            opp,
            data.get("slate_date") or today_str,
            (data.get("matchup_context") or {}).get("scheduled_start_utc"),
        )
        if times is None:
            continue
        snapshot_time, start_time = times
        venue = getattr(matched.market, "venue", None) or "polymarket"
        book = await router.get_top_of_book(
            venue=venue, token_id=str(token_id), market_id=matched.market.market_id
        )
        if book.get("best_ask") is None or book.get("best_bid") is None:
            continue
        fee = estimate_fee_per_share(venue, float(book["best_ask"]), 1.0)
        features = SportsEventFeatures(
            sport="mlb",
            league="mlb",
            event_id=f"mlb_outs_{pitcher}_{today_str}",
            market_id=matched.market.market_id,
            team_a=str(team),
            team_b=str(opp),
            start_time=start_time,
            snapshot_time=snapshot_time,
            market_prob_baseline=float(book["best_ask"]),
            market_price_source=f"{venue}_top_of_book",
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
                "contract_type": "pitcher_outs",
                "market_type": "pitcher_outs",
                "strategy": "mlb_pitcher_outs",
                "model_prob_override": model_prob,
                "outcome_token_id": str(token_id),
                **model_meta,
            },
        )
        sync_sports_market(
            market_data={
                "platform": venue,
                "model_prob_override": model_prob,
                "outcome_id": str(token_id),
            },
            features=features,
            best_ask=float(book["best_ask"]),
            best_bid=float(book["best_bid"]),
            spread=float(book["best_ask"]) - float(book["best_bid"]),
            fee_per_share=fee,
            visible_depth=float(book.get("ask_size") or 0.0),
            bankroll=bankroll,
            risk_caps=risk_caps,
            mode="shadow",
            real_orderbook_timestamp=book.get("book_timestamp"),
            real_received_timestamp=book.get("received_timestamp")
            or datetime.now(timezone.utc),
            outcome_id=str(token_id),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_mlb_shadow_execution())
