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

    # 1. Read-only test
    logger.info("Running read-only test...")
    try:
        res = client_us.portfolio.positions()
        logger.info(f"Read-only test passed. Fetched portfolio positions.")
    except Exception as read_exc:
        logger.error(f"Read portfolio failed: {read_exc}")
            
    # 2. Live-fire test
    logger.info("Running live-fire test...")
    try:
        events = client_us.events.list(params={"active": True, "closed": False, "limit": 1})
        if events and getattr(events, 'data', None) and len(events.data) > 0:
            event = events.data[0]
            markets = event.get("markets", [])
            if markets:
                market = markets[0]
                token_id = market.get("tokens", [{}])[0].get("token_id")
                
                if token_id:
                    price = 0.05
                    size = 10 # $0.50
                    logger.info(f"Placing test order: token={token_id}, price={price}, size={size}")
                    order = client_us.orders.create(token_id=token_id, price=price, size=size, side="BUY")
                    logger.info(f"Order created successfully: {order}")
                    
                    # Immediately try to cancel it
                    order_id = order.get("id") or order.get("orderID")
                    if order_id:
                        client_us.orders.cancel(order_id)
                        logger.info("Test order cancelled successfully.")
                else:
                    logger.warning("No token ID found in market.")
    except Exception as exc:
        logger.error(f"Live-fire test failed: {exc}")

    logger.info("All tests completed.")

if __name__ == "__main__":
    asyncio.run(run_test())
