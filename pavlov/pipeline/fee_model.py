from typing import Literal, Optional
from loguru import logger

def estimate_fee_per_share(
    platform: str,
    price: float,
    shares: float,
    market_type: Optional[str] = None,
    liquidity_role: Literal["maker", "taker"] = "taker",
    api_fee_override: Optional[float] = None
) -> float:
    if api_fee_override is not None:
        logger.debug(f"fee_source='api_override', platform='{platform}', fee={api_fee_override}")
        return api_fee_override

    platform_lower = platform.lower()
    
    if platform_lower == "kalshi":
        # Kalshi taker fee estimate: ~0.07 * C * P * (1-P).
        if price <= 0.0 or price >= 1.0:
            fee = 0.0
        else:
            fee = 0.07 * price * (1.0 - price)
        logger.debug(f"fee_source='static_fallback', platform='kalshi', fee={fee}")
        return fee
        
    elif platform_lower == "polymarket":
        # Polymarket typically charges ~2% on matched volume
        fee = 0.02 * price
        logger.debug(f"fee_source='static_fallback', platform='polymarket', fee={fee}")
        return fee
        
    logger.error(f"FEE_MODEL_UNAVAILABLE for platform {platform}")
    raise ValueError(f"FEE_MODEL_UNAVAILABLE: No fee model for {platform}")
