"""
Standalone sync script — runs the full data pipeline without needing uvicorn.
Used by GitHub Actions to keep data fresh every 30 minutes.

Modes:
  default      — full pipeline (scrape + ML)
  --scrape-only — match sync + scrapers only (fast CI cadence)
  --ml-only     — linking, Elo, consensus, calibration, autobet (slower cadence)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def run_scrape_phase() -> dict[str, int]:
    from backend.config import get_settings
    from backend.scrapers.covers_scraper import CoversScraper
    from backend.scrapers.youtube_scraper import YouTubeScraper
    from backend.scrapers.actionnetwork_scraper import ActionNetworkScraper
    from backend.scrapers.pickswise_scraper import PickswiseScraper
    from backend.sports_data.mlb_fetcher import sync_mlb_matches
    from backend.sports_data.worldcup_fetcher import sync_matches

    settings = get_settings()
    fast = settings.sync_fast_mode or os.getenv("SYNC_FAST", "").lower() in (
        "1", "true", "yes",
    )
    skip_yt_search = settings.sync_skip_youtube_search or os.getenv(
        "SYNC_SKIP_YT_SEARCH", "",
    ).lower() in ("1", "true", "yes")

    print("=== Scrape phase ===" + (" [fast]" if fast else ""))

    print("Syncing WC matches...")
    wc = await sync_matches()
    print(f"  {wc} matches upserted")

    print("Syncing MLB games...")
    mlb = await sync_mlb_matches()
    print(f"  {mlb} games upserted")

    print("Scraping Covers.com...")
    covers_picks = await CoversScraper().scrape_all()
    print(f"  {covers_picks} picks saved")

    print("Scraping YouTube...")
    yt_picks = await YouTubeScraper().scrape_all(skip_search=skip_yt_search)
    print(f"  {yt_picks} picks saved")

    print("Scraping ActionNetwork...")
    an_picks = await ActionNetworkScraper().scrape_all(fast=fast)
    print(f"  {an_picks} picks saved")

    print("Scraping Pickswise MLB...")
    pw_picks = await PickswiseScraper().scrape_all()
    print(f"  {pw_picks} picks saved")

    tw_picks = 0
    if settings.twitter_auth_token and settings.twitter_ct0:
        print("Scraping Twitter/X...")
        from backend.scrapers.twitter_scraper import TwitterScraper, seed_twitter_influencers
        await seed_twitter_influencers()
        tw_picks = await TwitterScraper().scrape_all()
        print(f"  {tw_picks} picks saved")
    else:
        print("Twitter/X skipped — add TWITTER_AUTH_TOKEN + TWITTER_CT0 to .env")

    tt_picks = 0
    if settings.tiktok_session_id or settings.tiktok_ms_token:
        print("Scraping TikTok...")
        try:
            from backend.scrapers.tiktok_scraper import TikTokScraper, seed_tiktok_influencers
            await seed_tiktok_influencers()
            tt_picks = await TikTokScraper().scrape_all()
            print(f"  {tt_picks} picks saved")
        except RuntimeError as exc:
            print(f"  TikTok skipped — {exc}")
    else:
        print("TikTok skipped — add TIKTOK_SESSION_ID or TIKTOK_MS_TOKEN to .env")

    return {
        "wc": wc,
        "mlb": mlb,
        "covers": covers_picks,
        "yt": yt_picks,
        "an": an_picks,
        "pw": pw_picks,
        "tw": tw_picks,
        "tt": tt_picks,
    }


async def run_ml_phase() -> dict[str, int]:
    from backend.ml.calibration import run_calibration
    from backend.ml.consensus_engine import compute_all_consensus
    from backend.ml.elo_ranker import (
        deactivate_poor_performers,
        sync_influencer_pick_counts,
        update_all_elo_scores,
    )

    from backend.notifications.discord_alerts import send_autobet_signals, send_consensus_alerts
    from backend.sports_data.mlb_fetcher import link_mlb_picks_to_matches
    from backend.sports_data.mlb_stats_fetcher import enrich_upcoming_mlb_pitcher_stats
    from backend.sports_data.pick_resolver import resolve_all_pending_picks
    from backend.sports_data.stats_sync import enrich_openfootball_ht, sync_match_stats
    from backend.sports_data.worldcup_fetcher import link_picks_to_matches
    from backend.trading.autobet import resolve_autobets, run_autobet
    from backend.trading.clv import compute_average_clv, snapshot_pick_market_probs

    print("=== ML phase ===")

    print("Fetching match stats for settlement...")
    stats_synced = await sync_match_stats()
    ht_enriched = await enrich_openfootball_ht()
    print(f"  stats synced={stats_synced} ht enriched={ht_enriched}")

    print("Enriching MLB probable pitcher stats...")
    sp_enriched = await enrich_upcoming_mlb_pitcher_stats()
    print(f"  pitcher stats enriched={sp_enriched}")

    print("Linking picks to matches...")
    linked_wc = await link_picks_to_matches()
    linked_mlb = await link_mlb_picks_to_matches()
    print(f"  linked WC={linked_wc} MLB={linked_mlb}")

    resolved_picks = resolve_all_pending_picks()
    print(f"  picks resolved={resolved_picks}")

    print("Snapshotting market lines (CLV)...")
    snapped = 0
    try:
        snapped = await snapshot_pick_market_probs()
        compute_average_clv()
        print(f"  {snapped} picks snapshotted")
    except Exception as exc:
        print(f"  CLV snapshot skipped: {exc}")

    print("Updating ML scores...")
    sync_influencer_pick_counts()
    deactivate_poor_performers()
    update_all_elo_scores()
    compute_all_consensus()

    print("Running calibration...")
    cal = run_calibration()
    if cal:
        print(
            f"  {cal.get('total_resolved', 0)} resolved | "
            f"Brier={cal.get('brier_score', 0):.4f} | "
            f"ROI={cal.get('simulated_roi_pct', 0):.1f}%"
        )

    print("Running weather prediction model (Phase 1)...")
    from backend.models.weather.sync_weather import sync_weather_predictions
    try:
        await sync_weather_predictions()
    except Exception as exc:
        print(f"  Weather sync failed: {exc}")

    print("Running MLB Quant Model Orchestrator (Phase 3)...")
    try:
        from backend.ml.mlb_quant.orchestrator import setup_daily_slate
        setup_daily_slate()
    except Exception as exc:
        print(f"  MLB Orchestrator failed: {exc}")

    print("Running Polymarket autobet...")
    autobet_summary: dict = {}
    try:
        autobet_summary = await run_autobet()
        print(
            f"  [{autobet_summary.get('mode')}] "
            f"placed={autobet_summary.get('placed', 0)} "
            f"rejected={autobet_summary.get('rejected', 0)}"
        )
    except Exception as exc:
        print(f"  Autobet placement skipped: {exc}")

    try:
        from backend.trading.autobet import resolve_autobets
        ab_resolved = resolve_autobets()
        print(f"  autobets resolved={ab_resolved}")
    except Exception as exc:
        print(f"  Autobet resolution failed: {exc}")
        
    try:
        from backend.trading.weather_settlement import resolve_weather_autobets
        w_resolved = await resolve_weather_autobets()
        print(f"  weather bets resolved={w_resolved}")
    except Exception as exc:
        print(f"  Weather autobet resolution failed: {exc}")
    print("Sending Discord alerts...")
    alerts = await send_consensus_alerts()
    signal_sent = 0
    if autobet_summary.get("signals"):
        signal_sent = await send_autobet_signals(autobet_summary["signals"])
    print(f"  {alerts} consensus alerts, {signal_sent} autobet signals sent")

    return {
        "linked": linked_wc + linked_mlb,
        "resolved": resolved_picks,
        "autobet": autobet_summary.get("placed", 0),
        "stats": stats_synced,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="SportsPick data sync")
    parser.add_argument(
        "--scrape-only",
        action="store_true",
        help="Match sync + scrapers only (no ML/calibration/autobet)",
    )
    parser.add_argument(
        "--ml-only",
        action="store_true",
        help="ML pipeline only (linking, Elo, consensus, calibration, autobet)",
    )
    args = parser.parse_args()

    if args.scrape_only and args.ml_only:
        print("Cannot use --scrape-only and --ml-only together")
        sys.exit(1)

    print("=== SportsPick Sync ===")
    scrape_stats: dict[str, int] = {}
    ml_stats: dict[str, int] = {}

    if not args.ml_only:
        scrape_stats = await run_scrape_phase()
    if not args.scrape_only:
        ml_stats = await run_ml_phase()

    if args.scrape_only:
        print(
            f"=== Scrape done: WC={scrape_stats.get('wc', 0)} "
            f"MLB={scrape_stats.get('mlb', 0)} "
            f"Covers={scrape_stats.get('covers', 0)} YT={scrape_stats.get('yt', 0)} "
            f"AN={scrape_stats.get('an', 0)} PW={scrape_stats.get('pw', 0)} ==="
        )
    elif args.ml_only:
        print(
            f"=== ML done: linked={ml_stats.get('linked', 0)} "
            f"resolved={ml_stats.get('resolved', 0)} "
            f"autobet={ml_stats.get('autobet', 0)} ==="
        )
    else:
        print(
            f"=== Done: WC={scrape_stats.get('wc', 0)} MLB={scrape_stats.get('mlb', 0)} "
            f"Covers={scrape_stats.get('covers', 0)} YT={scrape_stats.get('yt', 0)} "
            f"AN={scrape_stats.get('an', 0)} PW={scrape_stats.get('pw', 0)} "
            f"linked={ml_stats.get('linked', 0)} resolved={ml_stats.get('resolved', 0)} "
            f"autobet={ml_stats.get('autobet', 0)} ==="
        )


if __name__ == "__main__":
    asyncio.run(main())
