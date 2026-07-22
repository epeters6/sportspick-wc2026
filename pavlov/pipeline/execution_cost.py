from typing import List, Optional, Tuple
from dataclasses import dataclass
from loguru import logger

@dataclass
class AskLevel:
    price: float
    size: float

@dataclass
class ExecutableBucket:
    bucket_id: str
    ask_levels: List[AskLevel]
    best_bid: float
    best_ask: float
    spread: float

from pavlov.pipeline.fee_model import estimate_fee_per_share

# Opt-in only: evidence paths must pass default_depth_if_missing=None (the default)
# so missing ask size yields depth 0 / INSUFFICIENT_DEPTH rather than assumed size.
_DEFAULT_SHADOW_ASK_DEPTH = 50.0


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
    return p


def _extract_ask_size(m: dict, default_depth_if_missing: Optional[float]) -> float:
    for key in ("ask_size", "yes_ask_size", "yes_ask_qty", "ask_qty"):
        raw = m.get(key)
        if raw is None:
            continue
        try:
            size = float(raw)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    if default_depth_if_missing is not None and default_depth_if_missing > 0:
        return float(default_depth_if_missing)
    return 0.0


def generate_executable_cost_vector(
    raw_markets: List[dict],
    platform: str,
    slippage_buffer: float = 0.005,
    default_depth_if_missing: Optional[float] = None,
) -> Tuple[List[float], List[float]]:
    """
    Given a list of raw markets (mutually exclusive buckets), calculate the 
    Q_exec vector representing the marginal cost to execute one YES contract,
    and extract the available depth at that ask level.
    Q_exec = ask_price + fee + slippage

    Missing/zero ask size yields depth 0 (no assumed depth). Callers may
    explicitly pass default_depth_if_missing (e.g. _DEFAULT_SHADOW_ASK_DEPTH)
    only for non-evidence exploratory paths.
    """
    Q_exec = []
    depth_caps = []
    
    for m in raw_markets:
        ask = _as_probability(m.get("best_ask", m.get("yes_ask", 0.0)))
        ask_size = _extract_ask_size(m, default_depth_if_missing)
        
        # Explicit guard against midpoint usage (Audit #6)
        if m.get("execution_price_source") in {"mid", "last_trade", "mark"}:
            raise ValueError("Executable cost cannot use midpoint/last/mark price")
        
        # If there is no valid ask, we price it extremely high so the optimizer ignores it
        if ask <= 0.0 or ask >= 1.0:
            Q_exec.append(1.0)
            depth_caps.append(0.0)
            continue
            
        fee = estimate_fee_per_share(platform, ask, 1.0)
        effective_cost = ask + fee + slippage_buffer
        
        if effective_cost >= 1.0:
            raise ValueError(f"EFFECTIVE_COST_NOT_TRADABLE: Bucket ask={ask} plus fee/slippage yields cost {effective_cost} >= 1.0")
        
        if ask_size <= 0:
            logger.debug(
                "Missing/zero ask depth for %s — depth cap 0 (no assumed fill)",
                m.get("ticker") or m.get("id") or "unknown",
            )
            ask_size = 0.0

        Q_exec.append(effective_cost)
        depth_caps.append(ask_size)
        
    return Q_exec, depth_caps
