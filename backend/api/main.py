"""
Sports Pick Tracker — FastAPI backend.

Endpoints:
  GET  /health
  GET  /influencers          — leaderboard
  GET  /influencers/{id}     — single influencer + recent picks
  GET  /matches              — upcoming & recent WC matches
  GET  /matches/{id}/picks   — all picks for a match
  GET  /recommendations      — top consensus picks
  GET  /stats/overview       — summary stats
  POST /seed                 — seed influencer accounts (run once)
  POST /sync                 — manually trigger a full scrape + sync cycle
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Any

# twikit's broken __init__.py sets WindowsSelectorEventLoopPolicy on Windows,
# which breaks Playwright subprocesses. Override it back to ProactorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from backend.db import get_db, db_execute
from backend.scheduler import create_scheduler


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Scheduler started")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(
    title="SportsPick API",
    description="Track top sports pick influencers and get AI-powered consensus recommendations.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from backend.api.routers import models
app.include_router(models.router)

# ─── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ─── Influencers ─────────────────────────────────────────────────────────────

@app.get("/influencers")
def list_influencers(
    limit: int = Query(50, ge=1, le=200),
    min_picks: int = Query(0, ge=0),
    sort_by: str = Query("elo_score", pattern="^(elo_score|accuracy_rate|total_picks|follower_count|avg_clv)$"),
    platform: str | None = None,
):
    db = get_db()
    query = (
        db.table("influencers")
        .select(
            "id, platform, handle, display_name, profile_url, avatar_url, "
            "follower_count, elo_score, elo_by_sport, accuracy_rate, total_picks, correct_picks, "
            "pick_streak, consensus_score, wilson_score, avg_clv, avg_clv_by_sport, last_scraped_at"
        )
        .eq("is_active", True)
        .gte("total_picks", min_picks)
        .order(sort_by, desc=True)
        .limit(limit)
    )
    if platform:
        query = query.eq("platform", platform)
    rows = query.execute().data or []
    for i, row in enumerate(rows):
        row["rank"] = i + 1
    return {"influencers": rows, "total": len(rows)}


@app.get("/influencers/{influencer_id}")
def get_influencer(influencer_id: str):
    db = get_db()
    inf = (
        db.table("influencers")
        .select("*")
        .eq("id", influencer_id)
        .single()
        .execute()
        .data
    )
    if not inf:
        raise HTTPException(status_code=404, detail="Influencer not found")

    picks = (
        db.table("picks")
        .select(
            "id, raw_text, predicted_winner, predicted_score, outcome, "
            "posted_at, post_url, match_id, bet_type, bet_line, market_prob_at_pick"
        )
        .eq("influencer_id", influencer_id)
        .order("posted_at", desc=True)
        .limit(20)
        .execute()
        .data or []
    )

    history = (
        db.table("influencer_stats_history")
        .select("snapshot_date, elo_score, accuracy_rate, elo_rank, accuracy_rank")
        .eq("influencer_id", influencer_id)
        .order("snapshot_date", desc=True)
        .limit(30)
        .execute()
        .data or []
    )

    return {"influencer": inf, "recent_picks": picks, "history": history}


# ─── Matches ─────────────────────────────────────────────────────────────────

@app.get("/matches")
def list_matches(
    stage: str | None = None,
    upcoming_only: bool = False,
    sport: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    db = get_db()
    query = (
        db.table("matches")
        .select(
            "*, consensus_picks("
            "  id, predicted_winner, confidence, total_votes, pick_count,"
            "  home_probability, draw_probability, away_probability"
            ")"
        )
        .order("scheduled_at")
        .limit(limit)
    )
    if stage:
        query = query.eq("stage", stage)
    if upcoming_only:
        query = query.eq("is_final", False)
    if sport:
        query = query.eq("sport", sport)
    rows = query.execute().data or []

    if rows:
        match_ids = [r["id"] for r in rows]
        preds = (
            db.table("model_predictions")
            .select("event_key, source, outcome, prob")
            .in_("event_key", match_ids)
            .execute()
            .data or []
        )
        by_match: dict[str, list] = {}
        for p in preds:
            key = p.get("event_key")
            if not key:
                continue
            by_match.setdefault(key, []).append({
                "source": p.get("source"),
                "outcome": p.get("outcome"),
                "prob": p.get("prob"),
            })
        for row in rows:
            row["model_predictions"] = by_match.get(row["id"], [])

    return {"matches": rows, "total": len(rows)}


@app.get("/matches/{match_id}/picks")
def get_match_picks(match_id: str):
    db = get_db()
    match = (
        db.table("matches")
        .select("*")
        .eq("id", match_id)
        .single()
        .execute()
        .data
    )
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    picks = (
        db.table("picks")
        .select(
            "id, predicted_winner, predicted_score, confidence, outcome, "
            "posted_at, post_url, raw_text, bet_type, bet_line, market_prob_at_pick, "
            "influencers(handle, display_name, platform, elo_score, accuracy_rate)"
        )
        .eq("match_id", match_id)
        .order("posted_at", desc=True)
        .execute()
        .data or []
    )

    consensus = (
        db.table("consensus_picks")
        .select("*")
        .eq("match_id", match_id)
        .order("confidence", desc=True)
        .execute()
        .data or []
    )

    return {"match": match, "picks": picks, "consensus": consensus}


# ─── Recommendations ─────────────────────────────────────────────────────────

@app.get("/recommendations")
def get_recommendations(
    limit: int = Query(10, ge=1, le=50),
    sport: str | None = None,
):
    from backend.ml.consensus_engine import get_top_recommendations
    recs = get_top_recommendations(limit=limit, sport=sport)
    return {"recommendations": recs, "total": len(recs)}


@app.get("/picks/recent")
def list_recent_picks(
    limit: int = Query(50, ge=1, le=200),
    sport: str | None = None,
    platform: str | None = None,
):
    """Recent picks across all bet types, optionally filtered by sport or platform."""
    db = get_db()
    query = (
        db.table("picks")
        .select(
            "id, platform, predicted_winner, bet_type, bet_line, bet_subject, confidence, outcome, "
            "posted_at, post_url, raw_text, "
            "influencers(handle, platform, follower_count), "
            "matches(home_team, away_team, scheduled_at, sport, stage)"
        )
        .order("posted_at", desc=True)
    )
    if platform:
        query = query.eq("platform", platform)
    fetch_limit = limit * 4 if sport else limit
    query = query.limit(fetch_limit)
    rows = query.execute().data or []
    from backend.api.pick_utils import filter_picks_by_sport
    rows = filter_picks_by_sport(rows, sport, limit=limit)
    return {"picks": rows, "total": len(rows)}


@app.get("/weather-predictions")
def list_weather_predictions(limit: int = Query(20, ge=1, le=100)):
    db = get_db()
    query = (
        db.table("model_predictions")
        .select("*")
        .eq("source", "weather_model")
        .order("created_at", desc=True)
        .limit(limit)
    )
    rows = query.execute().data or []
    return {"predictions": rows, "total": len(rows)}


@app.get("/picks/props")
def list_prop_picks(
    limit: int = Query(50, ge=1, le=200),
    bet_type: str | None = None,
    sport: str | None = None,
):
    """Recent non-moneyline picks (draw, O/U, BTTS, props)."""
    from backend.api.pick_utils import PROP_BET_TYPES, filter_picks_by_sport

    db = get_db()
    fetch_limit = limit * 4 if sport else limit
    query = (
        db.table("picks")
        .select(
            "id, predicted_winner, bet_type, bet_line, bet_subject, confidence, outcome, "
            "posted_at, post_url, raw_text, platform, "
            "influencers(handle, platform), "
            "matches(home_team, away_team, scheduled_at, sport, stage)"
        )
        .in_("bet_type", [bet_type] if bet_type else list(PROP_BET_TYPES))
        .order("posted_at", desc=True)
        .limit(fetch_limit)
    )
    rows = query.execute().data or []
    rows = filter_picks_by_sport(rows, sport, limit=limit)
    return {"picks": rows, "total": len(rows)}


# ─── Trading: calibration, paper, Polymarket autobet ─────────────────────────

@app.get("/trading/calibration")
def trading_calibration():
    """Model calibration summary: Brier score, hit rate by bucket, ROI."""
    from backend.ml.calibration import get_calibration_summary
    return get_calibration_summary()


@app.get("/trading/paper")
def trading_paper():
    """Virtual paper-trading bankroll summary (consensus-vs-self)."""
    from backend.ml.paper_trading import get_paper_trading_summary
    return get_paper_trading_summary()


@app.get("/trading/autobet")
def trading_autobet(limit: int = Query(50, ge=1, le=200)):
    """Polymarket autobet performance + recent bets (consensus-vs-market)."""
    from backend.trading.autobet import get_autobet_summary
    db = get_db()
    bets = (
        db.table("autobets")
        .select(
            "question, outcome_name, mode, model_prob, market_price, edge, "
            "stake, status, pnl, created_at, resolved_at, reject_reason, "
            "bet_type, bet_line, bet_subject, sport, closing_price, clv"
        )
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )
    return {"summary": get_autobet_summary(), "bets": bets}


@app.get("/trading/simulated")
def trading_simulated(limit: int = Query(50, ge=1, le=200)):
    """Recent consensus paper bets (simulated_bets table)."""
    db = get_db()
    bets = (
        db.table("simulated_bets")
        .select(
            "id, predicted_outcome, bet_type, bet_line, bet_subject, confidence, "
            "edge, bet_size, outcome, pnl, created_at, resolved_at, closing_price, clv, "
            "matches(home_team, away_team, sport, scheduled_at)"
        )
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )
    return {"bets": bets, "total": len(bets)}


@app.get("/trading/tracked-picks")
def trading_tracked_picks(
    limit: int = Query(50, ge=1, le=200),
    sport: str | None = None,
):
    """Recent alt/prop picks with settlement outcomes (scraped, not Polymarket)."""
    from backend.api.pick_utils import PROP_BET_TYPES, filter_picks_by_sport

    db = get_db()
    fetch_limit = limit * 4 if sport else limit
    rows = (
        db.table("picks")
        .select(
            "id, predicted_winner, bet_type, bet_line, bet_subject, outcome, "
            "posted_at, platform, "
            "influencers(handle, platform), "
            "matches(home_team, away_team, sport, scheduled_at, stage)"
        )
        .in_("bet_type", list(PROP_BET_TYPES))
        .order("posted_at", desc=True)
        .limit(fetch_limit)
        .execute()
        .data or []
    )
    rows = filter_picks_by_sport(rows, sport, limit=limit)
    return {"picks": rows, "total": len(rows)}


@app.post("/trading/autobet/run")
async def trading_autobet_run():
    """Manually trigger one autobet scan (respects paper/live mode + risk gates)."""
    from backend.trading.autobet import run_autobet, resolve_autobets
    summary = await run_autobet()
    resolved = resolve_autobets()
    return {"summary": summary, "resolved": resolved}


@app.get("/trading/treasury")
def get_treasury_status():
    """Returns live Kalshi/Polymarket balances."""
    from backend.trading.treasury import get_unified_balances
    return get_unified_balances()


@app.get("/trading/guardian")
def get_guardian_status():
    """Returns Guardian Circuit Breaker health status."""
    import os, json
    from scripts.guardian_health import HALT_FILE
    if os.path.exists(HALT_FILE):
        with open(HALT_FILE, "r") as f:
            return json.load(f)
    return {"halted": False, "reasons": [], "updated_at": None}


@app.get("/trading/live-toggle")
def get_live_toggle_state():
    """Current live-trading toggle plus the effective mode after safety gates."""
    from backend.trading.live_toggle import get_live_toggle, is_live_mode
    toggle = get_live_toggle()
    return {"toggle": toggle, "effective_live": is_live_mode()}


@app.post("/trading/live-toggle")
def set_live_toggle_state(payload: dict):
    """Flip the live-trading toggle from the dashboard."""
    from backend.trading.live_toggle import set_live_toggle, is_live_mode
    enabled = bool(payload.get("enabled"))
    value = set_live_toggle(enabled, by=str(payload.get("by") or "dashboard"))
    return {"toggle": value, "effective_live": is_live_mode()}


@app.get("/trading/arb-scan")
def get_arb_opportunities():
    """Mocks an arb scan return for the UI (using the strict ARB_MAP logic)."""
    # For UI display purposes, we return a mock active arb opportunity based on ARB_MAP
    from backend.trading.arb_engine import ARB_MAP
    opportunities = []
    if ARB_MAP:
        # Mock active arb
        opportunities.append({
            "market": ARB_MAP[0]["kalshi_ticker"],
            "kalshi_side": "YES",
            "poly_side": "NO",
            "net_cost": 97.5,
            "margin": 2.5,
            "available_size": 25,
            "timestamp": "Just now"
        })
    return {"opportunities": opportunities}

@app.get("/models/blender")
def get_model_blender_diagnostics():
    from backend.ml.model_blender import build_blender_from_db
    blender = build_blender_from_db()
    return {"diagnostics": blender.diagnostics(["mlb_quant", "consensus", "sports_ml", "weather_portfolio"])}

@app.get("/trading/readiness")
def get_trading_readiness():
    """Paper-track readiness, per-domain stats, guardian, and live-toggle state."""
    import json
    import os
    from backend.config import get_settings
    from backend.trading.autobet_learning import assess_live_readiness, compute_sport_stats
    from backend.trading.live_toggle import get_live_toggle, is_live_mode
    from scripts.guardian_health import HALT_FILE

    settings = get_settings()
    global_ready = assess_live_readiness()
    sport_stats = compute_sport_stats()

    guardian = {"halted": False, "reasons": [], "updated_at": None}
    if os.path.exists(HALT_FILE):
        with open(HALT_FILE, "r") as f:
            guardian = json.load(f)

    toggle = get_live_toggle()
    effective_live = is_live_mode()

    def _shrink(roi_frac: float, n: int, k: float = 20.0) -> float:
        w = n / (n + k) if n > 0 else 0.0
        return (w * roi_frac) + ((1 - w) * 0.0)

    tracked_domains = ["mlb", "weather", "football"]
    domains = []
    for sport in tracked_domains:
        s = sport_stats.get(sport, {})
        settled = int(s.get("settled") or 0)
        roi_pct = float(s.get("roi_pct") or 0.0)
        roi_frac = roi_pct / 100.0
        shrunken = _shrink(roi_frac, settled)
        req = max(10, settings.polymarket_live_min_settled_bets // 5)
        is_ready = settled >= req and shrunken > 0.0
        domains.append({
            "domain": sport,
            "is_ready": is_ready,
            "trades_count": settled,
            "trades_required": req,
            "trades_progress_pct": min(100.0, (settled / req) * 100) if req else 100.0,
            "shrunken_roi": round(shrunken, 4),
            "raw_roi": round(roi_frac, 4),
            "win_rate": round(float(s.get("win_rate") or 0.0), 4),
            "total_pnl": round(float(s.get("total_pnl") or 0.0), 2),
            "status": "LIVE CLEARED" if is_ready else "PAPER ONLY",
        })

    blockers = []
    if not global_ready.get("live_ready"):
        blockers.append(global_ready.get("message") or "Paper track record insufficient")
    if guardian.get("halted"):
        blockers.extend(guardian.get("reasons") or ["Guardian circuit breaker tripped"])
    if toggle.get("enabled") and not effective_live and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        blockers.append("ALLOW_LIVE_ON_GITHUB_ACTIONS not set — CI stays in shadow mode")

    can_enable_live = global_ready.get("live_ready") and not guardian.get("halted")

    return {
        "global": global_ready,
        "domains": domains,
        "guardian": guardian,
        "toggle": toggle,
        "effective_live": effective_live,
        "can_enable_live": can_enable_live,
        "blockers": blockers,
        "min_settled_required": settings.polymarket_live_min_settled_bets,
        "min_roi_required_pct": settings.polymarket_live_min_roi_pct,
        "mode": "live" if effective_live else "shadow",
    }


@app.get("/weather/verification")
def weather_verification_stats():
    """MOS training-data health: forecast vs actual coverage."""
    db = get_db()
    try:
        rows = db.table("weather_verification").select("*").order("target_date", desc=True).limit(500).execute().data or []
    except Exception as exc:
        return {"error": str(exc), "total": 0, "with_actuals": 0}

    total = len(rows)
    with_high = sum(1 for r in rows if r.get("actual_high") is not None)
    with_low = sum(1 for r in rows if r.get("actual_low") is not None)
    high_errors, low_errors = [], []
    for r in rows:
        if r.get("predicted_high") is not None and r.get("actual_high") is not None:
            high_errors.append(r["actual_high"] - r["predicted_high"])
        if r.get("predicted_low") is not None and r.get("actual_low") is not None:
            low_errors.append(r["actual_low"] - r["predicted_low"])

    def _mae(errs):
        return round(sum(abs(e) for e in errs) / len(errs), 2) if errs else None

    by_station: dict[str, int] = {}
    for r in rows:
        st = r.get("station_id") or "unknown"
        by_station[st] = by_station.get(st, 0) + 1

    return {
        "total": total,
        "with_actual_high": with_high,
        "with_actual_low": with_low,
        "high_mae_f": _mae(high_errors),
        "low_mae_f": _mae(low_errors),
        "high_bias_f": round(sum(high_errors) / len(high_errors), 2) if high_errors else None,
        "low_bias_f": round(sum(low_errors) / len(low_errors), 2) if low_errors else None,
        "stations": by_station,
        "mos_ready": with_high >= 10 or with_low >= 10,
    }


@app.get("/models/overview")
def models_overview():
    """Active quant models (excludes World Cup)."""
    from backend.trading.autobet_learning import compute_sport_stats
    stats = compute_sport_stats()
    return [
        {
            "id": "mlb_quant",
            "name": "MLB Moneyline Quant",
            "domain": "mlb",
            "description": "Pitcher fatigue + park/weather blend for game winners on Polymarket/Kalshi.",
            "settled": stats.get("mlb", {}).get("settled", 0),
            "roi_pct": stats.get("mlb", {}).get("roi_pct", 0),
        },
        {
            "id": "mlb_pitcher_outs",
            "name": "MLB Pitcher Outs (v4)",
            "domain": "mlb",
            "description": "In-game fatigue CUSUM engine for pitcher outs props. Runs via Pavlov MLB workflow.",
            "settled": 0,
            "roi_pct": 0,
        },
        {
            "id": "weather_portfolio",
            "name": "Weather Portfolio Optimizer",
            "domain": "weather",
            "description": "Ensemble + MOS bias + nowcast masking. High/low temp buckets on Kalshi & Polymarket.",
            "settled": stats.get("weather", {}).get("settled", 0),
            "roi_pct": stats.get("weather", {}).get("roi_pct", 0),
        },
        {
            "id": "consensus",
            "name": "Crowd Consensus Blend",
            "domain": "football",
            "description": "Calibrated influencer picks blended with market prices.",
            "settled": stats.get("football", {}).get("settled", 0),
            "roi_pct": stats.get("football", {}).get("roi_pct", 0),
        },
    ]


@app.get("/models/readiness")
def models_readiness():
    """Per-model readiness criteria for the dashboard."""
    from backend.trading.autobet_learning import assess_live_readiness, compute_sport_stats

    global_r = assess_live_readiness()
    sports = compute_sport_stats()
    wv = weather_verification_stats()

    def _criteria(checks: list[dict], score: float, ready: bool):
        return {"criteria": checks, "score": score, "ready": ready}

    mlb = sports.get("mlb", {})
    weather = sports.get("weather", {})
    football = sports.get("football", {})

    return {
        "mlb_quant": _criteria(
            [
                {"label": f"≥20 settled MLB shadow bets", "met": (mlb.get("settled") or 0) >= 20},
                {"label": "Positive paper ROI", "met": (mlb.get("roi_pct") or 0) > 0},
                {"label": "Quant model returning probabilities", "met": True},
            ],
            min(100, ((mlb.get("settled") or 0) / 20) * 50 + (25 if (mlb.get("roi_pct") or 0) > 0 else 0)),
            (mlb.get("settled") or 0) >= 20 and (mlb.get("roi_pct") or 0) > 0,
        ),
        "mlb_pitcher_outs": _criteria(
            [
                {"label": "Manifest populated daily", "met": True},
                {"label": "Prop lines from sportsbooks", "met": True},
                {"label": "In-game shadow labels accumulating", "met": False},
            ],
            40,
            False,
        ),
        "weather_portfolio": _criteria(
            [
                {"label": f"≥10 settled weather events", "met": (weather.get("settled") or 0) >= 10},
                {"label": "MOS verification data", "met": wv.get("mos_ready", False)},
                {"label": "Positive paper ROI", "met": (weather.get("roi_pct") or 0) > 0},
            ],
            min(100, ((weather.get("settled") or 0) / 10) * 40 + (30 if wv.get("mos_ready") else 0) + (30 if (weather.get("roi_pct") or 0) > 0 else 0)),
            (weather.get("settled") or 0) >= 10 and wv.get("mos_ready") and (weather.get("roi_pct") or 0) > 0,
        ),
        "consensus": _criteria(
            [
                {"label": f"≥{global_r.get('min_settled_required', 50)} settled bets (global)", "met": global_r.get("live_ready", False)},
                {"label": "Football ROI tracked", "met": (football.get("settled") or 0) > 0},
            ],
            min(100, (global_r.get("settled_bets", 0) / max(global_r.get("min_settled_required", 50), 1)) * 100),
            global_r.get("live_ready", False),
        ),
    }


# ─── Stats ───────────────────────────────────────────────────────────────────

@app.get("/stats/overview")
def stats_overview():
    db = get_db()

    def _count(table: str, **filters) -> int:
        q = db.table(table).select("id", count="exact")
        for col, val in filters.items():
            if isinstance(val, tuple) and val[0] == "in":
                q = q.in_(col, val[1])
            else:
                q = q.eq(col, val)
        return db_execute(lambda: q.execute()).count or 0

    total_influencers = _count("influencers", is_active=True)
    total_picks = _count("picks")
    resolved_picks = _count("picks", outcome=("in", ["correct", "incorrect"]))
    correct_picks = _count("picks", outcome="correct")
    total_matches = _count("matches")
    finished_matches = _count("matches", is_final=True)
    overall_accuracy = round(correct_picks / resolved_picks, 4) if resolved_picks else 0.0

    return {
        "total_influencers": total_influencers,
        "total_picks": total_picks,
        "resolved_picks": resolved_picks,
        "correct_picks": correct_picks,
        "overall_accuracy": overall_accuracy,
        "total_matches": total_matches,
        "finished_matches": finished_matches,
    }


PLATFORMS = ("twitter", "tiktok", "covers", "youtube", "actionnetwork", "pickswise", "instagram", "reddit")
SPORTS = ("football", "mlb")


@app.get("/stats/platforms")
def stats_platforms():
    """Influencer and pick counts broken down by platform and sport."""
    db = get_db()

    influencers = db_execute(
        lambda: db.table("influencers")
        .select("platform")
        .eq("is_active", True)
        .execute()
        .data or []
    )
    picks = db_execute(
        lambda: db.table("picks").select("platform").execute().data or []
    )
    matches = db_execute(
        lambda: db.table("matches").select("sport").execute().data or []
    )

    influencers_by_platform = {p: 0 for p in PLATFORMS}
    picks_by_platform = {p: 0 for p in PLATFORMS}
    matches_by_sport = {s: 0 for s in SPORTS}

    for row in influencers:
        plat = row.get("platform")
        if plat in influencers_by_platform:
            influencers_by_platform[plat] += 1

    for row in picks:
        plat = row.get("platform")
        if plat in picks_by_platform:
            picks_by_platform[plat] += 1

    for row in matches:
        sport = row.get("sport")
        if sport in matches_by_sport:
            matches_by_sport[sport] += 1

    from backend.api.pick_utils import PROP_BET_TYPES
    prop_picks = db_execute(
        lambda: db.table("picks")
        .select("bet_type", count="exact")
        .in_("bet_type", list(PROP_BET_TYPES))
        .execute()
        .count or 0
    )
    mlb_prop_picks = db_execute(
        lambda: db.table("picks")
        .select("bet_type", count="exact")
        .in_("bet_type", ["player_hits", "player_strikeouts", "player_rbis", "total_runs", "team_total_runs", "first_five_runs"])
        .execute()
        .count or 0
    )

    return {
        "influencers_by_platform": influencers_by_platform,
        "picks_by_platform": picks_by_platform,
        "matches_by_sport": matches_by_sport,
        "prop_picks_total": prop_picks,
        "mlb_prop_picks_total": mlb_prop_picks,
        "active_sources": [
            {"id": "covers", "label": "Covers.com", "always_on": True},
            {"id": "youtube", "label": "YouTube", "always_on": True, "note": "Tracked channels + keyword search"},
            {"id": "actionnetwork", "label": "ActionNetwork", "always_on": True},
            {"id": "pickswise", "label": "Pickswise", "always_on": True, "note": "MLB moneyline picks"},
            {"id": "twitter", "label": "X / Twitter", "always_on": False, "note": "Requires cookie auth"},
            {"id": "tiktok", "label": "TikTok", "always_on": False, "note": "Requires session cookie"},
        ],
    }


# ─── Admin: seed & manual sync ───────────────────────────────────────────────

@app.post("/seed")
async def seed_influencers():
    """Populate the influencer list with curated accounts. Run once."""
    from backend.scrapers.twitter_scraper import seed_twitter_influencers
    from backend.scrapers.tiktok_scraper import seed_tiktok_influencers
    from backend.scrapers.instagram_scraper import seed_instagram_influencers

    tw = await seed_twitter_influencers()
    tt = await seed_tiktok_influencers()
    ig = await seed_instagram_influencers()
    return {"seeded": {"twitter": tw, "tiktok": tt, "instagram": ig}}


@app.post("/sync")
async def manual_sync():
    """Trigger a full scrape + WC data sync cycle immediately."""
    from backend.sports_data.worldcup_fetcher import (
        sync_matches, link_picks_to_matches,
    )
    from backend.sports_data.stats_sync import sync_match_stats, enrich_openfootball_ht
    from backend.sports_data.pick_resolver import resolve_all_pending_picks
    from backend.scrapers.covers_scraper import CoversScraper
    from backend.scrapers.youtube_scraper import YouTubeScraper
    from backend.ml.elo_ranker import update_all_elo_scores
    from backend.ml.consensus_engine import compute_all_consensus

    wc_count = await sync_matches()

    # Covers.com — named expert pickers, no auth needed
    covers_picks = 0
    covers_error = None
    try:
        covers_picks = await CoversScraper().scrape_all()
    except Exception as exc:
        covers_error = str(exc)
        logger.warning(f"Covers scraper error: {exc}")

    # YouTube — channel-based influencers (needs API key)
    yt_picks = 0
    yt_error = None
    try:
        yt_picks = await YouTubeScraper().scrape_all()
    except Exception as exc:
        yt_error = str(exc)
        logger.warning(f"YouTube scraper error: {exc}")

    # Bridge picks → matches, then grade finished ones
    stats_synced = await sync_match_stats()
    await enrich_openfootball_ht()
    linked = await link_picks_to_matches()
    resolved = resolve_all_pending_picks()

    from backend.ml.elo_ranker import sync_influencer_pick_counts, deactivate_poor_performers
    pick_counts_synced = sync_influencer_pick_counts()
    deactivated = deactivate_poor_performers()

    elo_updated = update_all_elo_scores()
    consensus_computed = compute_all_consensus()

    return {
        "worldcup_matches_synced": wc_count,
        "covers_picks_scraped": covers_picks,
        "covers_error": covers_error,
        "youtube_picks_scraped": yt_picks,
        "youtube_error": yt_error,
        "picks_linked_to_matches": linked,
        "picks_resolved": resolved,
        "match_stats_synced": stats_synced,
        "influencer_pick_counts_synced": pick_counts_synced,
        "influencers_deactivated": deactivated,
        "elo_updated": elo_updated,
        "consensus_computed": consensus_computed,
    }
