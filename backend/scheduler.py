"""
APScheduler-based job runner.

Jobs:
  Every 30 min  — scrape Covers.com + YouTube, sync WC matches, resolve picks
  Every 1 hour  — recompute Elo scores + consensus picks
  Daily 00:05   — snapshot daily stats + compute streaks
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from backend.config import get_settings

settings = get_settings()


async def job_scrape_all():
    """Full sync: WC matches → Covers → YouTube → link picks → resolve → ML."""
    from backend.sports_data.worldcup_fetcher import (
        sync_matches, link_picks_to_matches, resolve_pending_picks,
    )
    from backend.scrapers.covers_scraper import CoversScraper
    from backend.scrapers.youtube_scraper import YouTubeScraper
    from backend.ml.elo_ranker import (
        sync_influencer_pick_counts, update_all_elo_scores, deactivate_poor_performers,
    )
    from backend.ml.consensus_engine import compute_all_consensus

    try:
        await sync_matches()
    except Exception as exc:
        logger.error(f"WC sync failed: {exc}")

    try:
        covers = CoversScraper()
        await covers.scrape_all()
    except Exception as exc:
        logger.error(f"Covers scrape failed: {exc}")

    try:
        yt = YouTubeScraper()
        await yt.scrape_all()
    except Exception as exc:
        logger.error(f"YouTube scrape failed: {exc}")

    try:
        await link_picks_to_matches()
        await resolve_pending_picks()
    except Exception as exc:
        logger.error(f"Pick resolution failed: {exc}")

    try:
        sync_influencer_pick_counts()
        deactivate_poor_performers()
        update_all_elo_scores()
        compute_all_consensus()
    except Exception as exc:
        logger.error(f"ML update failed: {exc}")


def job_update_elo():
    from backend.ml.elo_ranker import update_all_elo_scores
    try:
        update_all_elo_scores()
    except Exception as exc:
        logger.error(f"Elo update job failed: {exc}")


def job_update_consensus():
    from backend.ml.consensus_engine import compute_all_consensus
    try:
        compute_all_consensus()
    except Exception as exc:
        logger.error(f"Consensus job failed: {exc}")


def job_daily_snapshot():
    from backend.ml.elo_ranker import snapshot_daily_stats
    from backend.ml.accuracy_scorer import compute_pick_streaks, compute_consensus_scores
    try:
        snapshot_daily_stats()
        compute_pick_streaks()
        compute_consensus_scores()
    except Exception as exc:
        logger.error(f"Daily snapshot job failed: {exc}")


def create_scheduler() -> AsyncIOScheduler:
    interval = settings.scrape_interval_minutes

    scheduler = AsyncIOScheduler(
        # Suppress "missed run" warnings — harmless on restart after downtime
        job_defaults={"misfire_grace_time": 60 * 10},
    )

    # ─── Main scrape + sync (every 30 min) ───────────────────────────────────
    scheduler.add_job(
        job_scrape_all,
        IntervalTrigger(minutes=interval),
        id="scrape_all",
        replace_existing=True,
        max_instances=1,
    )

    # ─── ML recompute (hourly) ────────────────────────────────────────────────
    scheduler.add_job(
        job_update_elo,
        IntervalTrigger(hours=1),
        id="update_elo",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        job_update_consensus,
        IntervalTrigger(hours=1),
        id="update_consensus",
        replace_existing=True,
        max_instances=1,
    )

    # ─── Daily snapshot ───────────────────────────────────────────────────────
    scheduler.add_job(
        job_daily_snapshot,
        CronTrigger(hour=0, minute=5),
        id="daily_snapshot",
        replace_existing=True,
        max_instances=1,
    )

    logger.info("Scheduler configured with all jobs")
    return scheduler
