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
    title="Sports Pick Tracker",
    description="Track top sports pick influencers and get AI-powered consensus recommendations.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
            "bet_type, bet_line, bet_subject, sport"
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
            "edge, bet_size, outcome, pnl, created_at, resolved_at, "
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
