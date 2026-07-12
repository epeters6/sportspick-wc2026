import os
import sys
from loguru import logger
import asyncio

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../pavlov")))
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from polymarket_us import PolymarketUS
from backend.config import get_settings
from backend.db import get_db

async def run_test():
    s = get_settings()
    logger.info("Initializing Polymarket US client...")
    try:
        client_us = PolymarketUS(
            key_id=s.polymarket_key_id, 
            secret_key=s.polymarket_secret_key
        )
        logger.info("Client initialized successfully.")
    except Exception as exc:
        logger.error(f"Failed to initialize client: {exc}")
        return

    # 1. Authenticated balance/portfolio fetch
    logger.info("Test 1: Authenticated Portfolio Fetch...")
    try:
        res = client_us.portfolio.positions()
        logger.info(f"Read-only portfolio test passed. Fetched {len(res) if hasattr(res, '__len__') else 'positions'}.")
    except Exception as read_exc:
        logger.error(f"Read portfolio failed: {read_exc}")
            
    # 2. Public order book fetch
    logger.info("Test 2: Public Order Book Fetch...")
    try:
        from backend.trading.polymarket_client import PolymarketClient
        pm = PolymarketClient()
        markets = await pm.fetch_markets(limit=5)
        success = False
        for market in markets:
            if market.outcomes:
                token_id = market.outcomes[0].token_id
                best_price, depth = await pm.get_book_depth(token_id)
                if best_price is not None:
                    logger.info(f"Public book test passed! Token={token_id}, best_price={best_price}, depth={depth}")
                    success = True
                    break
        if not success:
            logger.warning("Failed to find any book depth across multiple markets. (API may be fine, just empty books)")
    except Exception as exc:
        logger.error(f"Public book test failed: {exc}")

    logger.info("All tests completed.")

if __name__ == "__main__":
    asyncio.run(run_test())
