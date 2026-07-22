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
        series_ticker: str | None = None,
        limit: int = 200,
    ) -> List[PolyMarket]:
        """Fetch markets from both venues concurrently.

        Free-text ``search`` is Polymarket-only. Kalshi only accepts an explicit
        ``series_ticker`` (never a team name as series_ticker).
        """
        poly_task = self.poly.fetch_markets(tag_slug=tag_slug, search=search, limit=limit)
        kalshi_task = self.kalshi.fetch_markets(
            series_ticker=series_ticker,
            limit=limit,
        )

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

    async def fetch_mlb_moneyline_markets(self, *, poly_search: str, limit: int = 80) -> List[PolyMarket]:
        """Polymarket search + Kalshi KXMLBGAME events; local filter by matcher."""
        poly_task = self.poly.fetch_markets(search=poly_search, limit=limit)
        kalshi_task = self.kalshi.fetch_mlb_game_markets(limit=limit)
        results = await asyncio.gather(poly_task, kalshi_task, return_exceptions=True)
        out: List[PolyMarket] = []
        if isinstance(results[0], list):
            for m in results[0]:
                m.venue = "polymarket"
                out.append(m)
        if isinstance(results[1], list):
            for m in results[1]:
                m.venue = "kalshi"
                out.append(m)
        return out

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

    async def get_top_of_book(
        self,
        venue: str,
        token_id: str,
        market_id: str,
    ) -> dict:
        """Real top-of-book (bid/ask/size/timestamp). No fabricated fallbacks."""
        if venue.lower() == "kalshi":
            return await self.kalshi.get_top_of_book(token_id=token_id, market_id=market_id)
        return await self.poly.get_top_of_book(token_id=token_id)

    # Note: Order execution/splitting logic would go here, 
    # but for now we rely on autobet to select the venue and place the order.
