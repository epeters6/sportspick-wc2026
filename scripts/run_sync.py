"""
Standalone sync script — runs the full data pipeline without needing uvicorn.
Used by GitHub Actions to keep data fresh every 30 minutes.
"""
import asyncio
import sys
import os

# Make sure the backend package is importable from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from backend.sports_data.worldcup_fetcher import (
        sync_matches, link_picks_to_matches, resolve_pending_picks,
    )
    from backend.scrapers.covers_scraper import CoversScraper
    from backend.scrapers.youtube_scraper import YouTubeScraper
    from backend.scrapers.actionnetwork_scraper import ActionNetworkScraper
    from backend.ml.elo_ranker import (
        sync_influencer_pick_counts, update_all_elo_scores, deactivate_poor_performers,
    )
    from backend.ml.consensus_engine import compute_all_consensus

    print("=== SportsPick Sync ===")

    print("Syncing WC matches...")
    wc = await sync_matches()
    print(f"  {wc} matches upserted")

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

    print("Linking picks to matches...")
    linked = await link_picks_to_matches()
    resolved = await resolve_pending_picks()
    print(f"  {linked} linked, {resolved} resolved")

    print("Updating ML scores...")
    sync_influencer_pick_counts()
    deactivate_poor_performers()
    update_all_elo_scores()
    compute_all_consensus()

    print(f"=== Done: WC={wc} Covers={covers_picks} YT={yt_picks} AN={an_picks} linked={linked} resolved={resolved} ===")


if __name__ == "__main__":
    asyncio.run(main())
