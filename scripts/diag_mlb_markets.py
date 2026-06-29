import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from backend.trading.polymarket_client import PolymarketClient
    from backend.trading.market_matcher import _teams_in_text, map_outcome_to_token, is_per_match_market

    client = PolymarketClient()
    for term in ["Chicago Cubs Milwaukee", "Cubs Brewers moneyline", "Minnesota Twins win"]:
        markets = await client.fetch_markets(search=term, limit=40)
        print(f"\n=== search: {term!r} ({len(markets)}) ===")
        for m in markets[:15]:
            teams = _teams_in_text(m.question)
            per = is_per_match_market(m)
            outs = [o.name for o in m.outcomes]
            print(f"  per={per} teams={teams}")
            print(f"  Q: {m.question[:120]}")
            print(f"  outcomes: {outs}")


if __name__ == "__main__":
    asyncio.run(main())
