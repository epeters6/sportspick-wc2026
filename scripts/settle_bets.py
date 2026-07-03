"""Fetch stats + resolve pending picks (for manual runs)."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from backend.sports_data.stats_sync import sync_match_stats, enrich_openfootball_ht
    from backend.sports_data.worldcup_fetcher import link_picks_to_matches
    from backend.sports_data.mlb_fetcher import link_mlb_picks_to_matches
    from backend.sports_data.pick_resolver import resolve_all_pending_picks
    from backend.trading.autobet import resolve_autobets


    n = await sync_match_stats(limit=120)
    ht = await enrich_openfootball_ht()
    print(f"stats synced={n} ht={ht}")
    linked_wc = await link_picks_to_matches()
    linked_mlb = await link_mlb_picks_to_matches()
    print(f"linked wc={linked_wc} mlb={linked_mlb}")
    picks = resolve_all_pending_picks()
    autobets = resolve_autobets()
    print(f"resolved picks={picks} autobets={autobets}")


if __name__ == "__main__":
    asyncio.run(main())
