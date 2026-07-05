import asyncio
import logging
import time
from backend.trading.polymarket_client import PolymarketClient
from backend.trading.treasury import get_unified_balances
from backend.config import get_settings

logger = logging.getLogger(__name__)

# Strict Manual Equivalence Table
# NEVER use NLP/fuzzy matching to map markets for real-money arbitrage.
ARB_MAP = [
    {
        "kalshi_ticker": "KXFED-24DEC-0.25", 
        "poly_market_id": "0xabc123...", 
        "verified_by": "EPeters", 
        "verified_date": "2026-07-03",
        "notes": "Fed 25bps cut Dec 2024. Both settle on official FOMC press release."
    },
    {
        "kalshi_ticker": "KXPREZ-28-TRUMP",
        "poly_market_id": "0x456def...",
        "verified_by": "System",
        "verified_date": "2026-07-04",
        "notes": "2028 US Presidential Election - Donald Trump."
    },
    {
        "kalshi_ticker": "KXPREZ-28-HARRIS",
        "poly_market_id": "0x789ghi...",
        "verified_by": "System",
        "verified_date": "2026-07-04",
        "notes": "2028 US Presidential Election - Kamala Harris."
    }
]

def run_arb_scan():
    """
    Scans for cross-exchange arbitrage opportunities between Polymarket and Kalshi.
    """
    try:
        asyncio.run(_async_arb_scan())
    except Exception as exc:
        logger.error(f"Arb scan failed: {exc}")

async def _execute_arb_legs(kalshi_ticker, poly_market_id, side_kalshi, side_poly, size, expected_cost):
    """
    Simulates executing both legs near-simultaneously with leg-risk auto-unwind.
    """
    logger.info(f"⚡ EXECUTING ARB: {size} contracts. Kalshi {side_kalshi}, Poly {side_poly}.")
    
    # 1. Fire Kalshi Leg (usually faster/more reliable fiat API)
    kalshi_filled = True 
    
    # 2. Fire Polymarket Leg
    poly_filled = True
    
    if kalshi_filled and not poly_filled:
        logger.error(f"🚨 LEG RISK FAILURE: Kalshi filled, Poly failed. Auto-unwinding Kalshi position...")
        # Fire Kalshi Market Sell to unwind
        
    elif poly_filled and not kalshi_filled:
        logger.error(f"🚨 LEG RISK FAILURE: Poly filled, Kalshi failed. Auto-unwinding Poly position...")
        # Fire Poly Market Sell to unwind

async def _async_arb_scan():
    logger.info("Starting Strict Cross-Exchange Arbitrage Scan (Polymarket ↔ Kalshi)")
    
    s = get_settings()
    balances = get_unified_balances()
    
    # Dynamic treasury threshold based on position sizing
    expected_size = s.polymarket_bankroll * s.polymarket_max_position_pct
    min_treasury = expected_size * 3
    
    if balances["kalshi_usd"] < min_treasury or balances["polymarket_usdc"] < min_treasury:
        logger.warning(f"Arb Engine: Treasury below dynamic threshold (${min_treasury:.2f}). Skipping scan.")
        return
        
    if not ARB_MAP:
        logger.info("Arb Engine: ARB_MAP is empty. No verified markets to scan.")
        return
        
    poly_client = PolymarketClient()
    
    # Fees to net against the spread (e.g., Kalshi 1¢/contract, Poly 1%)
    KALSHI_FEE_CENTS = 1.0
    POLY_FEE_CENTS = 1.0 
    
    for pair in ARB_MAP:
        k_ticker = pair["kalshi_ticker"]
        p_id = pair["poly_market_id"]
        
        # Mock fetching Kalshi Orderbook
        # kalshi_ob = get_kalshi_orderbook(k_ticker)
        # kal_yes_ask = kalshi_ob.yes_ask
        # kal_no_ask = kalshi_ob.no_ask
        # kal_yes_depth = kalshi_ob.yes_depth
        
        # Mock fetching Polymarket Orderbook
        try:
            pm_market = await poly_client.get_market(p_id)
        except Exception:
            continue
            
        if not pm_market or len(pm_market.get("outcomes", [])) < 2:
            continue
            
        # Example dummy prices for structure
        kal_yes_ask = 50
        kal_no_ask = 55
        kal_yes_depth = 500
        kal_no_depth = 500
        
        pm_yes_ask = int((pm_market["outcomes"][0].get("best_ask") or 1.0) * 100)
        pm_no_ask = int((pm_market["outcomes"][1].get("best_ask") or 1.0) * 100)
        pm_yes_depth = 1000 # Dummy depth
        pm_no_depth = 1000
        
        # Arb Condition 1: Buy YES on Kalshi, NO on Polymarket
        cost_1 = kal_yes_ask + pm_no_ask
        net_cost_1 = cost_1 + KALSHI_FEE_CENTS + POLY_FEE_CENTS
        
        if net_cost_1 < 100:
            margin = 100 - net_cost_1
            available_size = min(kal_yes_depth, pm_no_depth)
            
            if available_size >= 10: # Depth-aware sizing filter
                logger.info(f"🚨 ARB FOUND! Buy YES Kalshi ({kal_yes_ask}¢), NO Poly ({pm_no_ask}¢). Net Cost: {net_cost_1}¢. Margin: {margin}¢. Max Size: {available_size}")
                await _execute_arb_legs(k_ticker, p_id, "YES", "NO", available_size, net_cost_1)
            
        # Arb Condition 2: Buy NO on Kalshi, YES on Polymarket
        cost_2 = kal_no_ask + pm_yes_ask
        net_cost_2 = cost_2 + KALSHI_FEE_CENTS + POLY_FEE_CENTS
        
        if net_cost_2 < 100:
            margin = 100 - net_cost_2
            available_size = min(kal_no_depth, pm_yes_depth)
            
            if available_size >= 10:
                logger.info(f"🚨 ARB FOUND! Buy NO Kalshi ({kal_no_ask}¢), YES Poly ({pm_yes_ask}¢). Net Cost: {net_cost_2}¢. Margin: {margin}¢. Max Size: {available_size}")
                await _execute_arb_legs(k_ticker, p_id, "NO", "YES", available_size, net_cost_2)
                
    logger.info("Arb Engine scan complete.")
