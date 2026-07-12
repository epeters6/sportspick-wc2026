"""
clv_tracker.py - Tracks and snapshots the Closing Line Value for autobets right before settlement.
"""
from __future__ import annotations
import logging
from backend.db import get_db
from backend.trading.polymarket_client import PolymarketClient

import os
import sys

# Add pavlov to path for poly_client / polymarket_us
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../pavlov")))
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from polymarket_us import PolymarketUS

logger = logging.getLogger(__name__)

async def snapshot_closing_prices():
    """
    Finds unresolved autobets that are near settlement or have just concluded,
    and records the final mid-quote (VWAP proxy) as the closing_price to calculate CLV.
    """
    db = get_db()
    
    # Fetch pending autobets (both placed and open, assuming we track CLV before settlement)
    bets = db.table("autobets").select("*").in_("status", ["placed", "open"]).is_("closing_price", "null").execute().data or []
    if not bets:
        return 0
        
    client_us = PolymarketUS(key_id="dummy", secret_key="dummy") # public read
    
    updated = 0
    total_pending = len(bets)
    
    for bet in bets:
        market_id = bet.get("market_id")
        if not market_id:
            continue
            
        try:
            # We assume all weather bets use PolymarketUS for now, or check venue/sport
            # If sport == "weather" or venue == "poly_us"
            bbo_data = client_us.markets.bbo(market_id)
            if not bbo_data or "marketData" not in bbo_data:
                continue
            
            market_data = bbo_data["marketData"]
            best_ask_data = market_data.get("bestAsk", {})
            best_bid_data = market_data.get("bestBid", {})
            
            best_ask = float(best_ask_data.get("value", 0))
            best_ask_size = float(best_ask_data.get("size", 0))
            
            best_bid = float(best_bid_data.get("value", 0))
            best_bid_size = float(best_bid_data.get("size", 0))
            
            # Liquidity check 1: Notional value of at least $25 on both sides
            ask_notional = best_ask * best_ask_size
            bid_notional = best_bid * best_bid_size
            has_size = ask_notional >= 25.0 and bid_notional >= 25.0
            
            # Liquidity check 2: Spread check
            mid_price = (best_ask + best_bid) / 2.0 if best_ask > 0 and best_bid > 0 else 0
            spread = best_ask - best_bid
            max_spread = max(0.02, 0.15 * mid_price)
            valid_spread = mid_price > 0 and spread <= max_spread
            
            if has_size and valid_spread:
                mid_quote = mid_price
            else:
                logger.debug(f"CLV Tracker: Skipping {market_id} due to thin liquidity (Ask Notional: ${ask_notional:.2f}, Bid Notional: ${bid_notional:.2f}, Spread: {spread:.3f})")
                continue
                
            # db update
            db.table("autobets").update({"closing_price": mid_quote}).eq("id", bet["id"]).execute()
            updated += 1
            logger.info(f"CLV Tracker: Recorded closing_price {mid_quote} for {market_id} (Trusted Liquidity)")
        except Exception as exc:
            logger.debug(f"CLV Tracker error for {market_id}: {exc}")
            
    logger.info(f"CLV Tracker completed: Updated {updated}/{total_pending} missing closing_prices.")
    if total_pending > 0:
        pass_rate = (updated / total_pending) * 100
        logger.info(f"CLV Liquidity Pass Rate: {pass_rate:.1f}% ({updated}/{total_pending})")
            
    return updated

def calculate_clv(entry_price: float, closing_price: float) -> float:
    """
    Computes Closing Line Value (CLV).
    Positive CLV means the bet beat the closing line (we bought cheaper than it closed).
    """
    return round(closing_price - entry_price, 4)

if __name__ == "__main__":
    import asyncio
    asyncio.run(snapshot_closing_prices())

