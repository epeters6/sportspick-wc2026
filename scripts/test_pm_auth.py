import asyncio
import os
import sys

# Add root path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from backend.trading.polymarket_client import PolymarketClient
from backend.config import get_settings

async def main():
    s = get_settings()
    print(f"Loaded credentials: Key ID starts with '{s.polymarket_key_id[:4]}' if set")
    
    print("\n--- Test 1: Authenticated Balance Fetch ---")
    try:
        # In polymarket_us SDK, get balance via client
        from polymarket_us import PolymarketUS
        client_us = PolymarketUS(key_id=s.polymarket_key_id, secret_key=s.polymarket_secret_key)
        # Not fully sure of the exact balance API in polymarket_us, but usually there's a portfolio or balance call.
        # Let's try to fetch active orders or something if balance isn't obvious. Or maybe just client.get_balance() if it exists.
        # Alternatively, we can use the 'users' endpoint or 'balances' endpoint.
        try:
            # try to call something authenticated to verify credentials
            print("Attempting to initialize PolymarketUS client and check auth...")
            # We can test by fetching open orders
            orders = client_us.orders.get_active_orders()
            print("Successfully authenticated! (Fetched active orders)")
        except Exception as e:
            print(f"Error during authenticated fetch: {e}")
    except Exception as e:
        print(f"Error initializing client: {e}")
        
    print("\n--- Test 2: Public Order Book Fetch ---")
    try:
        pm_client = PolymarketClient()
        # Find a market to check depth on
        markets = await pm_client.fetch_markets(limit=1)
        if markets and markets[0].outcomes:
            token = markets[0].outcomes[0].token_id
            print(f"Fetching public order book for token: {token}")
            best_price, depth = await pm_client.get_book_depth(token)
            if best_price is not None:
                print(f"Successfully fetched public book! Best price: {best_price}, Depth: {depth}")
            else:
                print("Failed to get book depth.")
        else:
            print("Failed to fetch markets for token ID.")
    except Exception as e:
        print(f"Error during public fetch: {e}")

if __name__ == "__main__":
    asyncio.run(main())
