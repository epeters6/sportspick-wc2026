from dataclasses import dataclass
from typing import Optional, Literal
from datetime import datetime, timezone
from pavlov.pipeline.trade_candidate import SizedOrder
from pavlov.pipeline.fee_model import estimate_fee_per_share
from loguru import logger

@dataclass
class PaperFill:
    market_id: str
    outcome_id: str
    side: str
    requested_shares: float
    filled_shares: float
    limit_price: float
    simulated_fill_price: float
    fees: float
    slippage: float
    visible_depth_used: float
    rejection_reason: Optional[str]
    is_partial: bool = False
    is_full_fill: bool = False
    unfilled_shares: float = 0.0
    partial_fill_reason: Optional[str] = None

def validate_orderbook_freshness(
    orderbook_timestamp: Optional[datetime],
    received_timestamp: Optional[datetime],
    mode: Literal["live", "shadow", "paper"] = "live",
    allow_assumed_fresh_orderbook_for_shadow: bool = False,
    max_orderbook_age_ms: int = 2000
) -> None:
    now = datetime.now(timezone.utc)
    
    if received_timestamp is None:
        if mode == "live":
            raise ValueError("MISSING_ORDERBOOK_TIMESTAMP")
        elif not allow_assumed_fresh_orderbook_for_shadow:
            raise ValueError("MISSING_ORDERBOOK_TIMESTAMP")
        else:
            logger.info("ORDERBOOK_TIMESTAMP_ASSUMED_FOR_SHADOW")
            return
            
    if received_timestamp.tzinfo is None:
        raise ValueError("received_timestamp must be timezone-aware")
        
    use_timestamp = orderbook_timestamp if orderbook_timestamp else received_timestamp
        
    if use_timestamp.tzinfo is None:
        raise ValueError("orderbook_timestamp must be timezone-aware")
    
    age_ms = (now - use_timestamp).total_seconds() * 1000.0
    
    if age_ms > max_orderbook_age_ms:
        logger.warning(f"STALE_ORDERBOOK: age is {age_ms:.0f}ms (max {max_orderbook_age_ms}ms)")
        raise ValueError("STALE_ORDERBOOK")

def reprice_and_validate(
    original_order: SizedOrder,
    new_best_ask: float,
    new_visible_depth: float,
    max_price_worsening: float = 0.005,
    max_depth_reduction_pct: float = 0.25
) -> SizedOrder:
    candidate = original_order.candidate
    
    # Check depth
    if new_visible_depth < candidate.visible_depth * (1.0 - max_depth_reduction_pct):
        raise ValueError("DEPTH_EVAPORATED")
        
    if new_visible_depth < original_order.target_shares:
        # Resize to fit depth if it evaporated
        new_shares = new_visible_depth
    else:
        new_shares = original_order.target_shares
        
    if new_shares <= 0:
        raise ValueError("DEPTH_EVAPORATED")
        
    # Check price
    new_fee = estimate_fee_per_share(candidate.platform, new_best_ask, 1.0)
    new_cost = new_best_ask + new_fee + candidate.slippage_buffer
    
    if new_cost > candidate.executable_cost + max_price_worsening:
        raise ValueError("PRICE_MOVED_AGAINST_US")
        
    # Re-verify edge
    p = candidate.model_prob if candidate.side == "YES" else (1.0 - candidate.model_prob)
    if p - new_cost <= 0:
        raise ValueError("EDGE_GONE_AFTER_REPRICE")
        
    return SizedOrder(
        candidate=candidate,
        target_shares=new_shares,
        target_cost=new_shares * new_cost,
        limit_price=new_cost,
        expected_log_growth_delta=original_order.expected_log_growth_delta, # approx
        rejection_reason=None
    )

def simulate_paper_fill(
    order: SizedOrder,
    orderbook_timestamp: Optional[datetime],
    received_timestamp: Optional[datetime],
    mode: Literal["live", "shadow", "paper"] = "live",
    allow_assumed_fresh_orderbook_for_shadow: bool = False
) -> PaperFill:
    try:
        validate_orderbook_freshness(
            orderbook_timestamp, 
            received_timestamp, 
            mode=mode, 
            allow_assumed_fresh_orderbook_for_shadow=allow_assumed_fresh_orderbook_for_shadow
        )
    except ValueError as e:
        return PaperFill(
            market_id=order.candidate.market_id,
            outcome_id=order.candidate.outcome_id,
            side=order.candidate.side,
            requested_shares=order.target_shares,
            filled_shares=0.0,
            limit_price=order.limit_price,
            simulated_fill_price=0.0,
            fees=0.0,
            slippage=0.0,
            visible_depth_used=0.0,
            rejection_reason=str(e),
            is_partial=False,
            is_full_fill=False,
            unfilled_shares=order.target_shares,
            partial_fill_reason=None
        )
        
    candidate = order.candidate
    best_ask = candidate.best_ask
    if best_ask is None:
        return PaperFill(
            market_id=candidate.market_id,
            outcome_id=candidate.outcome_id,
            side=candidate.side,
            requested_shares=order.target_shares,
            filled_shares=0.0,
            limit_price=order.limit_price,
            simulated_fill_price=0.0,
            fees=0.0,
            slippage=0.0,
            visible_depth_used=0.0,
            rejection_reason="NO_ASK_AVAILABLE",
            is_partial=False,
            is_full_fill=False,
            unfilled_shares=order.target_shares,
            partial_fill_reason=None
        )
        
    # Fill up to depth cap
    filled_shares = min(order.target_shares, candidate.visible_depth)
    if filled_shares <= 0:
        return PaperFill(
            market_id=candidate.market_id,
            outcome_id=candidate.outcome_id,
            side=candidate.side,
            requested_shares=order.target_shares,
            filled_shares=0.0,
            limit_price=order.limit_price,
            simulated_fill_price=0.0,
            fees=0.0,
            slippage=0.0,
            visible_depth_used=0.0,
            rejection_reason="INSUFFICIENT_DEPTH",
            is_partial=False,
            is_full_fill=False,
            unfilled_shares=order.target_shares,
            partial_fill_reason=None
        )
        
    fees = candidate.fee_per_share * filled_shares
    
    is_partial = filled_shares < order.target_shares
    is_full_fill = filled_shares == order.target_shares
    unfilled = order.target_shares - filled_shares
    partial_reason = "INSUFFICIENT_VISIBLE_DEPTH" if is_partial else None
    
    return PaperFill(
        market_id=candidate.market_id,
        outcome_id=candidate.outcome_id,
        side=candidate.side,
        requested_shares=order.target_shares,
        filled_shares=filled_shares,
        limit_price=order.limit_price,
        simulated_fill_price=best_ask,
        fees=fees,
        slippage=candidate.slippage_buffer * filled_shares,
        visible_depth_used=filled_shares,
        rejection_reason=None,
        is_partial=is_partial,
        is_full_fill=is_full_fill,
        unfilled_shares=unfilled,
        partial_fill_reason=partial_reason
    )
