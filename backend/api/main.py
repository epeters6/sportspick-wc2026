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

from backend.db import get_db
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
    sort_by: str = Query("elo_score", pattern="^(elo_score|accuracy_rate|total_picks|follower_count)$"),
    platform: str | None = Query(None, pattern="^(twitter|tiktok|instagram|covers|youtube)$"),
):
    db = get_db()
    query = (
        db.table("influencers")
        .select(
            "id, platform, handle, display_name, profile_url, avatar_url, "
            "follower_count, elo_score, accuracy_rate, total_picks, correct_picks, "
            "pick_streak, consensus_score, last_scraped_at"
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
        .select("id, raw_text, predicted_winner, predicted_score, outcome, posted_at, post_url, match_id")
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
    limit: int = Query(50, ge=1, le=200),
):
    db = get_db()
    query = (
        db.table("matches")
        .select("*, consensus_picks(predicted_winner, confidence, total_votes)")
        .order("scheduled_at")
        .limit(limit)
    )
    if stage:
        query = query.eq("stage", stage)
    if upcoming_only:
        query = query.eq("is_final", False)
    rows = query.execute().data or []
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
            "posted_at, post_url, raw_text, "
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
def get_recommendations(limit: int = Query(10, ge=1, le=50)):
    from backend.ml.consensus_engine import get_top_recommendations
    recs = get_top_recommendations(limit=limit)
    return {"recommendations": recs, "total": len(recs)}


# ─── Stats ───────────────────────────────────────────────────────────────────

@app.get("/stats/overview")
def stats_overview():
    db = get_db()
    total_influencers = (
        db.table("influencers").select("id", count="exact").eq("is_active", True).execute().count or 0
    )
    total_picks = (
        db.table("picks").select("id", count="exact").execute().count or 0
    )
    resolved_picks = (
        db.table("picks")
        .select("id", count="exact")
        .in_("outcome", ["correct", "incorrect"])
        .execute()
        .count or 0
    )
    correct_picks = (
        db.table("picks")
        .select("id", count="exact")
        .eq("outcome", "correct")
        .execute()
        .count or 0
    )
    total_matches = (
        db.table("matches").select("id", count="exact").execute().count or 0
    )
    finished_matches = (
        db.table("matches").select("id", count="exact").eq("is_final", True).execute().count or 0
    )
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
        sync_matches, resolve_pending_picks, link_picks_to_matches,
    )
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
    linked = await link_picks_to_matches()
    resolved = await resolve_pending_picks()

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
        "influencer_pick_counts_synced": pick_counts_synced,
        "influencers_deactivated": deactivated,
        "elo_updated": elo_updated,
        "consensus_computed": consensus_computed,
    }
