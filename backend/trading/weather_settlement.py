import asyncio
import httpx
from datetime import datetime, timezone
from loguru import logger
from backend.db import get_db
from backend.trading.kalshi_client import KalshiClient

import os
import sys

# Add pavlov to path for poly_client
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../pavlov")))
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from polymarket import poly_client

async def check_polymarket_resolution(slug: str) -> dict | None:
    """Check Polymarket US API for market resolution using poly_client."""
    if not poly_client.poly_configured():
        logger.warning("POLYMARKET_KEY_ID not set. Using dummy keys for public data.")
        poly_client.poly_configured = lambda: True
        original_get_client = poly_client.get_client
        def mock_get_client():
            from polymarket_us import PolymarketUS
            return PolymarketUS(key_id="dummy", secret_key="dummy")
        poly_client.get_client = mock_get_client

    try:
        # get_market_result returns 'yes' or 'no' if settled, else None
        res = poly_client.get_market_result(slug)
        if res:
            return {
                "closed": True,
                "active": False,
                "resolved": True,
                "winner": res
            }
    except Exception as e:
        logger.warning(f"Error fetching Polymarket resolution for {slug}: {e}")
    return None

async def check_kalshi_resolution(ticker: str) -> dict | None:
    """Check Kalshi API for market resolution."""
    client = KalshiClient()
    try:
        async with httpx.AsyncClient() as http_client:
            data = await client._get(http_client, f"/markets/{ticker}")
            if data and "market" in data:
                m = data["market"]
                return {
                    "closed": m.get("status") in ("closed", "settled", "determined"),
                    "resolved": m.get("status") == "settled",
                    "winner": m.get("result") # "yes", "no", or similar
                }
    except Exception as e:
        logger.warning(f"Error fetching Kalshi resolution for {ticker}: {e}")
    return None

async def resolve_weather_autobets():
    logger.info("Starting weather autobet resolution...")
    db = get_db()
    
    # Fetch all open weather bets
    open_bets = db.table("autobets").select("*").eq("bet_type", "weather").eq("status", "open").execute().data or []
    
    if not open_bets:
        logger.info("No open weather bets found.")
        return 0
        
    logger.info(f"Checking resolution for {len(open_bets)} open weather bets.")
    resolved_count = 0
    
    for bet in open_bets:
        venue = bet.get("venue", "polymarket").lower()
        market_id = bet.get("market_id")
        
        status_data = None
        if venue == "kalshi":
            status_data = await check_kalshi_resolution(market_id)
        else:
            status_data = await check_polymarket_resolution(market_id)
            
        if not status_data:
            continue
            
        if not status_data.get("resolved") and not status_data.get("closed"):
            continue
            
        winner = status_data.get("winner")
        if winner is None:
            continue
            
        # Determine if we won
        # For Polymarket, winner might be "Yes" or "No" or "0", "1"
        # For Kalshi, "yes" or "no"
        backed = str(bet.get("outcome_name")).lower()
        winner_str = str(winner).lower()
        
        # Simple heuristic: if winner equals our outcome, we won.
        # Polymarket sometimes sets winner to '1' (which is the index).
        # Assuming we usually bet 'yes'.
        won = False
        if winner_str in ("yes", "1", "true") and backed == "yes":
            won = True
        elif winner_str in ("no", "0", "false") and backed == "no":
            won = True
            
        stake = bet.get("stake") or 0.0
        shares = bet.get("shares") or 0.0
        price = bet.get("market_price") or 0.0
        
        new_status = "won" if won else "lost"
        new_pnl = round(shares * (1 - price), 2) if won else round(-stake, 2)
        
        try:
            db.table("autobets").update({
                "status": new_status,
                "pnl": new_pnl,
                "resolved_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", bet["id"]).execute()
            resolved_count += 1
            logger.info(f"Resolved weather bet {bet['id'][:8]} -> {new_status} (PnL: {new_pnl})")
        except Exception as e:
            logger.error(f"Failed to update weather bet {bet['id']}: {e}")
            
    logger.info(f"Resolved {resolved_count} weather bets.")
    return resolved_count

if __name__ == "__main__":
    asyncio.run(resolve_weather_autobets())
