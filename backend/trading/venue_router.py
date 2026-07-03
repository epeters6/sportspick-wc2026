import asyncio
from loguru import logger
from typing import List, Tuple

from backend.trading.polymarket_client import PolymarketClient, PolyMarket
from backend.trading.kalshi_client import KalshiClient

class VenueRouter:
    """
    Centralized router that fetches markets from Polymarket and Kalshi,
    and determines the best execution venue(s) for a given trade.
    """
    def __init__(self):
        self.poly = PolymarketClient()
        self.kalshi = KalshiClient()

    async def fetch_markets(
        self,
        *,
        tag_slug: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> List[PolyMarket]:
        """Fetch markets from both venues concurrently."""
        
        # We run both fetches in parallel.
        # Note: Kalshi uses `search` as a series ticker or title filter.
        poly_task = self.poly.fetch_markets(tag_slug=tag_slug, search=search, limit=limit)
        kalshi_task = self.kalshi.fetch_markets(tag_slug=tag_slug, search=search, limit=limit)

        results = await asyncio.gather(poly_task, kalshi_task, return_exceptions=True)
        
        all_markets = []
        if isinstance(results[0], list):
            for m in results[0]:
                m.venue = "polymarket"
                all_markets.append(m)
        else:
            logger.warning(f"VenueRouter: Polymarket fetch failed: {results[0]}")
            
        if isinstance(results[1], list):
            for m in results[1]:
                m.venue = "kalshi"
                all_markets.append(m)
        else:
            logger.warning(f"VenueRouter: Kalshi fetch failed: {results[1]}")

        logger.info(f"VenueRouter: found {len(all_markets)} total markets across venues.")
        return all_markets

    async def get_book_depth(
        self, 
        venue: str, 
        token_id: str, 
        market_id: str, 
        side: str = "sell"
    ) -> Tuple[float | None, float]:
        """Fetch book depth from the specified venue."""
        if venue.lower() == "kalshi":
            return await self.kalshi.get_book_depth(token_id=token_id, market_id=market_id, side=side)
        else:
            return await self.poly.get_book_depth(token_id=token_id, side=side)

    # Note: Order execution/splitting logic would go here, 
    # but for now we rely on autobet to select the venue and place the order.
