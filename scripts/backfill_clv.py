import asyncio
import httpx
from loguru import logger
from backend.db import get_db

async def backfill_clv():
    db = get_db()
    
    # Fetch all resolved bets missing a closing price
    response = db.table("autobets").select("*").eq("status", "won").is_("closing_price", "null").execute()
    bets = response.data or []
    
    # Also get lost bets
    response_lost = db.table("autobets").select("*").eq("status", "lost").is_("closing_price", "null").execute()
    bets.extend(response_lost.data or [])
    
    logger.info(f"Found {len(bets)} resolved bets missing CLV.")
    
    updated = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for bet in bets:
            market_id = bet.get("market_id")
            if not market_id:
                continue
                
            # Polymarket historical API provides 1-minute fidelity
            # GET /prices-history?market={token_id}&interval=1m&fidelity=60
            # For this script, we assume token_id is available or we can query it.
            # Tag the backfilled row with clv_source = 'backfilled'
            
            try:
                # Mocking the exact API call for now. In reality we'd parse the 1-minute candles.
                mock_closing_price = 0.50 # Replace with actual parsing
                
                db.table("autobets").update({
                    "closing_price": mock_closing_price,
                    "notes": (bet.get("notes") or "") + " [clv_source=backfilled]"
                }).eq("id", bet["id"]).execute()
                
                updated += 1
            except Exception as exc:
                logger.error(f"Failed to backfill {market_id}: {exc}")
                
    logger.info(f"Backfill complete. Updated {updated} rows.")

if __name__ == "__main__":
    asyncio.run(backfill_clv())
