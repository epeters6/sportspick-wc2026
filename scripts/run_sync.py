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
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def run_scrape_phase() -> dict[str, int]:
    from backend.config import get_settings
    from backend.scrapers.covers_scraper import CoversScraper
    from backend.scrapers.youtube_scraper import YouTubeScraper
    from backend.scrapers.actionnetwork_scraper import ActionNetworkScraper
    from backend.scrapers.pickswise_scraper import PickswiseScraper
    from backend.scrapers.silver_bulletin_scraper import sync_politics
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

    async def _safe(name: str, coro) -> int:
        """One broken scraper must not kill the whole scrape phase."""
        print(f"Scraping {name}...")
        try:
            n = await coro
            print(f"  {n} picks saved")
            return n
        except Exception as exc:
            print(f"  {name} scraper failed (continuing): {exc}")
            return 0

    covers_picks = await _safe("Covers.com", CoversScraper().scrape_all())
    yt_picks = await _safe("YouTube", YouTubeScraper().scrape_all(skip_search=skip_yt_search))
    an_picks = await _safe("ActionNetwork", ActionNetworkScraper().scrape_all(fast=fast))
    pw_picks = await _safe("Pickswise MLB", PickswiseScraper().scrape_all())

    print("Scraping Silver Bulletin (Politics)...")
    try:
        pol_picks = sync_politics()
        print(f"  {pol_picks} picks saved")
    except Exception as exc:
        pol_picks = 0
        print(f"  Silver Bulletin scraper failed (continuing): {exc}")

    tw_picks = 0
    if settings.twitter_auth_token and settings.twitter_ct0:
        print("Scraping Twitter/X...")
        try:
            from backend.scrapers.twitter_scraper import TwitterScraper, seed_twitter_influencers
            await seed_twitter_influencers()
            tw_picks = await TwitterScraper().scrape_all()
            print(f"  {tw_picks} picks saved")
        except Exception as exc:
            print(f"  Twitter/X scraper failed (continuing): {exc}")
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
    snapped = await snapshot_pick_market_probs()
    compute_average_clv()
    print(f"  {snapped} picks snapshotted")

    print("Updating ML scores...")
    sync_influencer_pick_counts()
    deactivate_poor_performers()
    update_all_elo_scores()

    print("Running WC quant model (team Elo + draw)...")
    try:
        from backend.ml.worldcup_quant import resolve_wc_quant_predictions, sync_wc_quant_predictions
        wc_written = sync_wc_quant_predictions()
        wc_resolved = resolve_wc_quant_predictions()
        print(f"  wc_quant predictions={wc_written} resolved={wc_resolved}")
    except Exception as exc:
        print(f"  WC quant failed: {exc}")

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
    try:
        # In-process so logs appear in the Actions step and repo-root imports work.
        # (Subprocess `python backend/models/weather/sync_weather.py` previously
        # failed with ModuleNotFoundError: backend, and check=False hid it.)
        from backend.models.weather.sync_weather import sync_weather_predictions

        await sync_weather_predictions()
        print("  Weather sync completed")
    except Exception as exc:
        print(f"  WEATHER SYNC FAILED: {exc}")
        traceback.print_exc()
        # Fail the ML step so green CI can no longer hide zero weather markets/bets.
        raise

    print("Running MLB Quant Model Orchestrator (Phase 3)...")
    try:
        from backend.ml.mlb_quant.orchestrator import setup_daily_slate
        setup_daily_slate()
        # NOTE: MLB shadow execution now runs once per ML cycle via
        # scripts/run_sports_shadow_validation.py (sync_ml.yml step) —
        # running it here too doubled the work and the Actions minutes.
    except Exception as exc:
        print(f"  MLB Orchestrator failed: {exc}")
        traceback.print_exc()
        raise

    print("Running Polymarket autobet...")
    autobet_summary: dict = {}
    try:
        # --- GUARDIAN HEALTH CHECK (required — fail closed) ---
        print("  Running Guardian Health Check...")
        from scripts.guardian_health import check_health
        check_health()

        # --- TREASURY & ARBITRAGE (optional) ---
        print("  Running Treasury & Arb Scans...")
        try:
            from backend.trading.treasury import check_treasury_health
            from backend.trading.arb_engine import run_arb_scan
            check_treasury_health()
            run_arb_scan()
        except Exception as exc:
            print(f"  Treasury/Arb warning (continuing): {exc}")

        # autobet (live vs paper dictated by config) — placement is required path
        autobet_summary = await run_autobet()
        print(
            f"  [{autobet_summary.get('mode')}] "
            f"placed={autobet_summary.get('placed', 0)} "
            f"rejected={autobet_summary.get('rejected', 0)}"
        )
    except Exception as exc:
        print(f"  Autobet/Guardian failed: {exc}")
        traceback.print_exc()
        raise

    # CLV closing prices + settlement are required (fail CI)
    from backend.trading.autobet import update_closing_prices, resolve_autobets
    clv_updated = await update_closing_prices()
    print(f"  clv updated={clv_updated} open bets")

    ab_resolved = resolve_autobets()
    print(f"  autobets resolved={ab_resolved}")

    from backend.trading.weather_settlement import resolve_weather_autobets
    w_resolved = await resolve_weather_autobets()
    print(f"  weather bets resolved={w_resolved}")

    from backend.ml.weather_verification import backfill_actuals
    wv_backfilled = backfill_actuals()
    print(f"  weather verification backfilled={wv_backfilled}")

    # Optional alerts — warn only
    print("Sending Discord alerts...")
    try:
        alerts = await send_consensus_alerts()
        signal_sent = 0
        if autobet_summary.get("signals"):
            signal_sent = await send_autobet_signals(autobet_summary["signals"])
        print(f"  {alerts} consensus alerts, {signal_sent} autobet signals sent")
    except Exception as exc:
        print(f"  Discord alerts warning (continuing): {exc}")
        alerts = 0
        signal_sent = 0

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
    from datetime import datetime
    start_time = datetime.now()
    import json
    status_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_status.json")
    
    def write_status(exit_code=0, error_msg=None, completed=False, mode="live"):
        duration = (datetime.now() - start_time).total_seconds()
        status_data = {
            "last_started_at": start_time.isoformat(),
            "last_finished_at": datetime.now().isoformat() if completed or error_msg else None,
            "last_duration_seconds": duration,
            "last_exit_code": exit_code,
            "last_status": "success" if exit_code == 0 and not error_msg else "failed" if error_msg else "running",
            "last_error": error_msg,
            "mode": mode,
            "mlb_shadow_started": False,
            "mlb_shadow_completed": False,
            "clv_scheduler_once_completed": False,
            "report_written": None
        }
        try:
            with open(status_file, "w") as f:
                json.dump(status_data, f, indent=2)
        except:
            pass
            
    # Mode reflects actual trading mode, not which pipeline phase ran.
    try:
        from backend.trading.live_toggle import is_live_mode
        mode = "live" if is_live_mode() else "shadow"
    except Exception:
        mode = "shadow"
    write_status(mode=mode)
    
    try:
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
        print(
            f"=== Done: WC={scrape_stats.get('wc', 0)} MLB={scrape_stats.get('mlb', 0)} "
            f"Covers={scrape_stats.get('covers', 0)} YT={scrape_stats.get('yt', 0)} "
            f"AN={scrape_stats.get('an', 0)} PW={scrape_stats.get('pw', 0)} "
            f"linked={ml_stats.get('linked', 0)} resolved={ml_stats.get('resolved', 0)} "
            f"autobet={ml_stats.get('autobet', 0)} ==="
        )
        write_status(completed=True, mode=mode)
    except Exception as e:
        write_status(exit_code=1, error_msg=str(e), completed=False, mode=mode)
        raise


if __name__ == "__main__":
    asyncio.run(main())
