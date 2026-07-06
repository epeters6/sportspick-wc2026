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
                # Fetch actual 1m candles for the token
                url = f"https://clob.polymarket.com/prices-history?market={bet.get('token_id', market_id)}&interval=1m&fidelity=60"
                resp = await client.get(url)
                
                if resp.status_code == 200:
                    data = resp.json()
                    history = data.get("history", [])
                    if history and len(history) > 0:
                        mock_closing_price = history[-1].get("p", 0.50)
                    else:
                        continue
                else:
                    logger.warning(f"Failed to fetch history for {market_id}: HTTP {resp.status_code}")
                    continue
                
                clv = bet.get("model_prob", 0) - mock_closing_price
                db.table("autobets").update({
                    "closing_price": mock_closing_price,
                    "clv": clv,
                    "clv_source": "backfilled"
                }).eq("id", bet["id"]).execute()
                
                updated += 1
            except Exception as exc:
                logger.error(f"Failed to backfill {market_id}: {exc}")
                
    logger.info(f"Backfill complete. Updated {updated} rows.")

if __name__ == "__main__":
    asyncio.run(backfill_clv())
