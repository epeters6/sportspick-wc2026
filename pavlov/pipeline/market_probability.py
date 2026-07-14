from typing import List, Tuple
from loguru import logger
from pavlov.pipeline.probability_model import validate_probability_vector

# Default Lambda values per implementation plan
LAMBDA_DEFAULTS = {
    "same_day_with_reliable_nowcast": 0.55,
    "day_ahead_near_mean": 0.45,
    "day_ahead_tail_bucket": 0.30,
    "two_day_near_mean": 0.35,
    "two_day_tail_bucket": 0.25,
    "three_to_five_day": 0.20,
}

def get_event_lambda(lead_days: int) -> float:
    """Get the Bayesian shrinkage confidence factor based on lead time."""
    if lead_days <= 0:
        return LAMBDA_DEFAULTS["same_day_with_reliable_nowcast"]
    elif lead_days == 1:
        return LAMBDA_DEFAULTS["day_ahead_near_mean"]
    elif lead_days == 2:
        return LAMBDA_DEFAULTS["two_day_near_mean"]
    else:
        return LAMBDA_DEFAULTS["three_to_five_day"]

def _as_probability(price) -> float:
    """Normalize venue quotes to [0, 1]. Clients often store cents (1–100)."""
    if price is None:
        return 0.0
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.0
    if p > 1.0:
        p = p / 100.0
    return max(0.0, min(1.0, p))


def generate_market_implied_vector(raw_markets: List[dict]) -> List[float]:
    """
    Given a list of raw markets (representing mutually exclusive buckets for an event),
    extract the implied probability from the bid/ask spread (or last trade) and normalize 
    it into a coherent market distribution.
    """
    if not raw_markets:
        return []
        
    P_market_raw = []
    
    for m in raw_markets:
        # Polymarket usually has 'best_bid', 'best_ask'. Kalshi might have 'yes_bid', 'yes_ask'.
        # Quotes may be fractions or cents — normalize before blending.
        bid = _as_probability(m.get("best_bid", m.get("yes_bid", 0.0)))
        ask = _as_probability(m.get("best_ask", m.get("yes_ask", 0.0)))
        
        if bid > 0 and ask > 0 and ask > bid:
            mid = (bid + ask) / 2.0
        elif ask > 0:
            mid = max(0.0, ask - 0.01)
        elif bid > 0:
            mid = min(1.0, bid + 0.01)
        else:
            # Fallback to last trade price for probability shrinkage ONLY
            mid = _as_probability(m.get("last_trade_price", m.get("last_price", 0.0)))
            
        P_market_raw.append(max(0.0, float(mid)))
        
    total_raw = sum(P_market_raw)
    
    # Normalize to 1.0
    if total_raw <= 0:
        raise ValueError("NO_ORDERBOOK_LIQUIDITY: Market probabilities sum to 0")
        
    P_market = [p / total_raw for p in P_market_raw]
        
    validate_probability_vector("P_market", P_market)
    return P_market


def shrink_probability_vector(
    P_model: List[float], 
    P_market: List[float], 
    lead_days: int
) -> List[float]:
    """
    Apply Bayesian shrinkage to the full vector.
    P_adj = lambda * P_model + (1 - lambda) * P_market
    """
    if len(P_model) != len(P_market):
        raise ValueError(f"Vector length mismatch: P_model {len(P_model)} != P_market {len(P_market)}")
        
    lambda_confidence = get_event_lambda(lead_days)
    
    P_adj = []
    for p_mod, p_mkt in zip(P_model, P_market):
        p_adj = (lambda_confidence * p_mod) + ((1.0 - lambda_confidence) * p_mkt)
        P_adj.append(p_adj)
        
    validate_probability_vector("P_adj", P_adj)
    return P_adj
