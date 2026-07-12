from typing import List
from dataclasses import dataclass

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

def generate_executable_cost_vector(
    raw_markets: List[dict],
    platform: str,
    slippage_buffer: float = 0.005
) -> Tuple[List[float], List[float]]:
    """
    Given a list of raw markets (mutually exclusive buckets), calculate the 
    Q_exec vector representing the marginal cost to execute one YES contract,
    and extract the available depth at that ask level.
    Q_exec = ask_price + fee + slippage
    """
    Q_exec = []
    depth_caps = []
    
    for m in raw_markets:
        ask = m.get("best_ask", m.get("yes_ask", 0.0))
        ask_size = m.get("ask_size", m.get("yes_ask_size", 0.0))
        
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
        
        Q_exec.append(effective_cost)
        depth_caps.append(ask_size)
        
    return Q_exec, depth_caps
