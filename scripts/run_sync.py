"""
Standalone sync script — runs the full data pipeline without needing uvicorn.
Used by GitHub Actions to keep data fresh every 30 minutes.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from backend.sports_data.worldcup_fetcher import (
        sync_matches, link_picks_to_matches,
    )
    from backend.sports_data.mlb_fetcher import (
        sync_mlb_matches, link_mlb_picks_to_matches,
    )
    from backend.config import get_settings
    from backend.scrapers.covers_scraper import CoversScraper
    from backend.scrapers.youtube_scraper import YouTubeScraper
    from backend.scrapers.actionnetwork_scraper import ActionNetworkScraper
    from backend.ml.elo_ranker import (
        sync_influencer_pick_counts, update_all_elo_scores, deactivate_poor_performers,
    )
    from backend.ml.consensus_engine import compute_all_consensus
    from backend.ml.calibration import run_calibration
    from backend.ml.paper_trading import place_paper_bets, resolve_paper_bets
    from backend.trading.clv import snapshot_pick_market_probs, compute_average_clv
    from backend.trading.autobet import run_autobet, resolve_autobets
    from backend.notifications.discord_alerts import send_consensus_alerts, send_autobet_signals

    print("=== SportsPick Sync ===")

    # ── Match data ────────────────────────────────────────────────────────────
    print("Syncing WC matches...")
    wc = await sync_matches()
    print(f"  {wc} matches upserted")

    print("Syncing MLB games...")
    mlb = await sync_mlb_matches()
    print(f"  {mlb} games upserted")

    # ── Scrapers ──────────────────────────────────────────────────────────────
    print("Scraping Covers.com...")
    covers = CoversScraper()
    covers_picks = await covers.scrape_all()
    print(f"  {covers_picks} picks saved")

    print("Scraping YouTube...")
    yt = YouTubeScraper()
    yt_picks = await yt.scrape_all()
    print(f"  {yt_picks} picks saved")

    print("Scraping ActionNetwork...")
    an = ActionNetworkScraper()
    an_picks = await an.scrape_all()
    print(f"  {an_picks} picks saved")

    settings = get_settings()
    tw_picks = 0
    if settings.twitter_auth_token and settings.twitter_ct0:
        print("Scraping Twitter/X...")
        from backend.scrapers.twitter_scraper import TwitterScraper, seed_twitter_influencers
        await seed_twitter_influencers()
        tw = TwitterScraper()
        tw_picks = await tw.scrape_all()
        print(f"  {tw_picks} picks saved")
    else:
        print("Twitter/X skipped — add TWITTER_AUTH_TOKEN + TWITTER_CT0 to .env (free, cookie-based)")

    tt_picks = 0
    if settings.tiktok_session_id or settings.tiktok_ms_token:
        print("Scraping TikTok...")
        try:
            from backend.scrapers.tiktok_scraper import TikTokScraper, seed_tiktok_influencers
            await seed_tiktok_influencers()
            tt = TikTokScraper()
            tt_picks = await tt.scrape_all()
            print(f"  {tt_picks} picks saved")
        except RuntimeError as exc:
            print(f"  TikTok skipped — {exc}")
    else:
        print("TikTok skipped — add TIKTOK_SESSION_ID or TIKTOK_MS_TOKEN to .env")

    # ── Stats + linking & resolution ──────────────────────────────────────────
    print("Fetching match stats for settlement...")
    from backend.sports_data.stats_sync import sync_match_stats, enrich_openfootball_ht
    stats_synced = await sync_match_stats()
    ht_enriched = await enrich_openfootball_ht()
    print(f"  stats synced={stats_synced} ht enriched={ht_enriched}")

    print("Linking picks to matches...")
    linked_wc = await link_picks_to_matches()
    linked_mlb = await link_mlb_picks_to_matches()
    print(f"  linked WC={linked_wc} MLB={linked_mlb}")

    from backend.sports_data.pick_resolver import resolve_all_pending_picks
    resolved_picks = resolve_all_pending_picks()
    print(f"  picks resolved={resolved_picks}")

    # ── CLV snapshot (market line at observation time) ─────────────────────────
    # Runs before Elo so resolved picks carry a market_prob_at_pick for CLV scoring.
    print("Snapshotting market lines (CLV)...")
    try:
        snapped = await snapshot_pick_market_probs()
        compute_average_clv()
        print(f"  {snapped} picks snapshotted")
    except Exception as exc:
        print(f"  CLV snapshot skipped: {exc}")

    # ── ML scoring ────────────────────────────────────────────────────────────
    print("Updating ML scores...")
    sync_influencer_pick_counts()
    deactivate_poor_performers()
    update_all_elo_scores()
    compute_all_consensus()

    # ── Calibration ───────────────────────────────────────────────────────────
    print("Running calibration...")
    cal = run_calibration()
    if cal:
        brier = cal.get("brier_score", 0)
        roi = cal.get("simulated_roi_pct", 0)
        total = cal.get("total_resolved", 0)
        print(f"  {total} resolved | Brier={brier:.4f} | ROI={roi:.1f}%")

    # ── Paper trading (consensus-vs-self, virtual bankroll) ────────────────────
    print("Paper trading...")
    bets_placed = place_paper_bets()
    bets_resolved = resolve_paper_bets()
    print(f"  {bets_placed} new bets, {bets_resolved} resolved")

    # ── Polymarket autobet (consensus-vs-market, paper unless live enabled) ─────
    print("Running Polymarket autobet...")
    autobet_summary = {}
    try:
        autobet_summary = await run_autobet()
        print(f"  [{autobet_summary.get('mode')}] "
              f"placed={autobet_summary.get('placed', 0)} "
              f"rejected={autobet_summary.get('rejected', 0)}")
    except Exception as exc:
        print(f"  Autobet placement skipped: {exc}")

    # Always settle open bets — even if placement failed
    try:
        ab_resolved = resolve_autobets()
        print(f"  autobets resolved={ab_resolved}")
    except Exception as exc:
        print(f"  Autobet resolution failed: {exc}")

    # ── Discord alerts ────────────────────────────────────────────────────────
    print("Sending Discord alerts...")
    alerts = await send_consensus_alerts()
    signal_sent = 0
    if autobet_summary.get("signals"):
        signal_sent = await send_autobet_signals(autobet_summary["signals"])
    print(f"  {alerts} consensus alerts, {signal_sent} autobet signals sent")

    print(
        f"=== Done: WC={wc} MLB={mlb} "
        f"Covers={covers_picks} YT={yt_picks} AN={an_picks} X={tw_picks} TT={tt_picks} "
        f"linked={linked_wc+linked_mlb} resolved={resolved_picks} "
        f"autobet={autobet_summary.get('placed', 0)} ==="
    )


if __name__ == "__main__":
    asyncio.run(main())
