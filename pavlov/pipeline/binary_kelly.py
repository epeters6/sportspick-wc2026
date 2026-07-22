from typing import Literal
from pavlov.pipeline.trade_candidate import TradeCandidate, SizedOrder
from pavlov.pipeline.risk_caps import RiskCaps
import math
from loguru import logger

def binary_kelly_fraction(
    model_prob: float,
    executable_cost: float,
    side: Literal["YES", "NO"] = "YES",
) -> float:
    if executable_cost <= 0.0 or executable_cost >= 1.0:
        return 0.0

    if side == "YES":
        p = model_prob
        c = executable_cost
    else:
        p = 1.0 - model_prob
        c = executable_cost

    f = (p - c) / (1.0 - c)
    return max(0.0, f)

def expected_binary_log_growth(
    f: float,
    p: float,
    c: float
) -> float:
    """Expected log growth for dollar-fraction Kelly.

    ``f`` is dollars spent / bankroll (not share count). Binary contract
    pays $1 if win, so relative wealth is:
      win:  1 - f + f/c
      lose: 1 - f
    """
    if f <= 0:
        return 0.0
    if c <= 0.0 or c >= 1.0:
        return 0.0
    if f >= 1.0:
        return -float("inf")
    wealth_if_win = 1.0 - f + f / c
    wealth_if_loss = 1.0 - f
    if wealth_if_win <= 0.0 or wealth_if_loss <= 0.0:
        return -float("inf")
    return p * math.log(wealth_if_win) + (1.0 - p) * math.log(wealth_if_loss)

def size_binary_trade(
    candidate: TradeCandidate,
    kelly_fraction: float,
    risk_caps: RiskCaps,
) -> SizedOrder:
    if kelly_fraction <= 0.0:
        return SizedOrder(candidate, 0.0, 0.0, candidate.executable_cost, 0.0, "NEGATIVE_EDGE")

    # 1. Start with full Kelly recommended shares
    target_cost_dollars = kelly_fraction * candidate.bankroll
    target_shares = target_cost_dollars / candidate.executable_cost

    # 2. Enforce limits
    max_shares_by_depth = candidate.max_shares_by_depth
    max_shares_by_event = risk_caps.get_event_exposure_cap_dollars(candidate.bankroll) / candidate.executable_cost
    max_shares_by_outcome = risk_caps.get_outcome_exposure_cap_dollars(candidate.bankroll) / candidate.executable_cost
    max_shares_by_strategy = risk_caps.get_strategy_exposure_cap_dollars(candidate.bankroll) / candidate.executable_cost
    max_shares_by_platform = risk_caps.get_platform_exposure_cap_dollars(candidate.bankroll) / candidate.executable_cost

    # Minimum of all caps
    final_shares = min(
        target_shares,
        max_shares_by_depth,
        max_shares_by_event,
        max_shares_by_outcome,
        max_shares_by_strategy,
        max_shares_by_platform
    )

    final_shares = math.floor(final_shares)
    final_cost = final_shares * candidate.executable_cost

    if final_shares <= 0:
        return SizedOrder(candidate, 0.0, 0.0, candidate.executable_cost, 0.0, "ZERO_SHARES_AFTER_ROUNDING_OR_CAPS")

    # 3. Re-verify edge and log growth
    p = candidate.model_prob if candidate.side == "YES" else (1.0 - candidate.model_prob)
    c = candidate.executable_cost
    
    net_edge = p - c
    if net_edge < risk_caps.min_net_edge:
        return SizedOrder(candidate, 0.0, 0.0, candidate.executable_cost, 0.0, "BELOW_MINIMUM_EDGE")

    f_realized = final_cost / candidate.bankroll
    log_growth_delta = expected_binary_log_growth(f_realized, p, c)

    if log_growth_delta < risk_caps.min_log_growth_delta:
        return SizedOrder(candidate, 0.0, 0.0, candidate.executable_cost, log_growth_delta, "BELOW_MINIMUM_LOG_GROWTH")

    return SizedOrder(
        candidate=candidate,
        target_shares=final_shares,
        target_cost=final_cost,
        limit_price=candidate.executable_cost,
        expected_log_growth_delta=log_growth_delta,
        rejection_reason=None
    )
