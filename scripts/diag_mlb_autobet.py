"""Quick MLB Polymarket match diagnostic."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from backend.db import get_db
    from backend.trading.autobet import _find_market_for_winner, _markets_for_match
    from backend.trading.polymarket_client import PolymarketClient

    db = get_db()
    cons = db.table("consensus_picks").select("*, matches(*)").execute().data or []
    client = PolymarketClient()
    markets_by_id: dict = {}
    for tag in ["mlb", "baseball", "sports"]:
        for m in await client.fetch_markets(tag_slug=tag, limit=100):
            markets_by_id[m.market_id] = m
    for term in ["MLB", "Red Sox", "Cubs", "Twins", "Brewers", "Mariners"]:
        for m in await client.fetch_markets(search=term, limit=30):
            markets_by_id.setdefault(m.market_id, m)
    markets = list(markets_by_id.values())
    print("markets_fetched", len(markets))

    for c in cons:
        m = c.get("matches") or {}
        if m.get("sport") != "mlb":
            continue
        match = m
        w = c["predicted_winner"]
        conf = c.get("confidence")
        found = await _find_market_for_winner(client, match, w, markets, markets_by_id)
        ms = await _markets_for_match(client, match, markets, markets_by_id)
        ht = m.get("home_team")
        at = m.get("away_team")
        status = "FOUND" if found else "NO MARKET"
        print(f"{status} | {at} @ {ht} | pick={w} conf={conf} matched={len(ms)}")
        for x in ms[:5]:
            print("  Q:", (x.question or "")[:100])
            print("  outcomes:", [o.name for o in x.outcomes[:4]])


if __name__ == "__main__":
    asyncio.run(main())
