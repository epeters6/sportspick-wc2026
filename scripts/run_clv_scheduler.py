import asyncio
import os
import sys
from datetime import datetime, timezone
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pavlov.pipeline.clv_updater import update_clv_checkpoints
from backend.trading.polymarket_client import PolymarketClient
from backend.trading.kalshi_client import KalshiClient
from backend.trading.venue_router import VenueRouter

async def _fetch_price_async(market_id: str, outcome_id: str, side: str, router: VenueRouter) -> float | None:
    venue = "kalshi" if market_id.startswith("KX-") else "polymarket"
    
    # We can get book depth which gives best_price and size
    try:
        best_price, _ = await router.get_book_depth(venue=venue, token_id=outcome_id, market_id=market_id, side=side.lower())
        return best_price
    except Exception as e:
        logger.warning(f"Failed to fetch price for CLV {market_id} {outcome_id}: {e}")
        return None

async def run_scheduler(once: bool = False):
    logger.info("Starting CLV Checkpoint Scheduler...")
    router = VenueRouter()
    
    while True:
        try:
            logger.info("Running update_clv_checkpoints...")
            
            # Update generic sports/weather tracking
            await update_clv_checkpoints(
                fetch_price=lambda mid, oid, s: _fetch_price_async(mid, oid, s, router),
                filepath="sports_clv_tracking.jsonl"
            )
            
            # Weather might be in another file, or we just pass the default
            if os.path.exists("clv_tracking.jsonl"):
                await update_clv_checkpoints(
                    fetch_price=lambda mid, oid, s: _fetch_price_async(mid, oid, s, router),
                    filepath="clv_tracking.jsonl"
                )
                
        except Exception as e:
            logger.error(f"Error in CLV Scheduler: {e}")
            
        if once:
            logger.info("Running once, exiting.")
            break
            
        logger.info("Sleeping for 60 seconds...")
        await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        run_once = "--once" in sys.argv
        asyncio.run(run_scheduler(once=run_once))
    except KeyboardInterrupt:
        logger.info("CLV Scheduler stopped manually.")
