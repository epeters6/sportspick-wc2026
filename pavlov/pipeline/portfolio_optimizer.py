import numpy as np
from scipy.optimize import minimize
from typing import List, Tuple
from loguru import logger

def expected_log_growth(x: np.ndarray, p_adj: np.ndarray, q_exec: np.ndarray, bankroll: float) -> float:
    """
    Calculate the negative expected log-growth of the bankroll (negative because we minimize).
    x: vector of shares bought for each bucket
    p_adj: vector of adjusted probabilities for each bucket
    q_exec: vector of execution costs per share for each bucket
    """
    total_cost = np.sum(q_exec * x)
    
    # If the total cost exceeds bankroll, return a massive penalty
    if total_cost >= bankroll:
        return 1e9
        
    # Payout if bucket j wins is x_j
    # Post-resolution bankroll for outcome j: B - total_cost + x_j
    post_bankrolls = bankroll - total_cost + x
    
    # Avoid log(<=0)
    if np.any(post_bankrolls <= 0):
        return 1e9
        
    log_b = np.log(post_bankrolls)
    
    # Expected log growth: sum(p_j * log(B_j))
    expected_log_B = np.sum(p_adj * log_b)
    
    return -expected_log_B

def optimize_portfolio(
    P_adj: List[float],
    Q_exec: List[float],
    depth_caps: List[float],
    bankroll: float,
    min_net_edge: float = 0.015
) -> List[float]:
    """
    Solve the mutually exclusive Kelly portfolio optimization problem.
    Returns the vector of shares to buy for each bucket.
    """
    p_adj = np.array(P_adj)
    q_exec = np.array(Q_exec)
    n_buckets = len(p_adj)
    
    # Filter out objectively terrible bets to help the optimizer
    # (If net edge < min_net_edge, we shouldn't bet it)
    net_edges = p_adj - q_exec
    
    # Initial guess: 0 shares
    x0 = np.zeros(n_buckets)
    
    # Event-level bankroll cap (e.g. 2% of total bankroll)
    event_bankroll_cap = 0.02 * bankroll
    
    # Bounds for each x_i: between 0 and a bucket position cap
    bounds = []
    for i in range(n_buckets):
        if net_edges[i] < min_net_edge or q_exec[i] >= 1.0 or depth_caps[i] <= 0:
            bounds.append((0.0, 0.0))  # Force 0
        else:
            # max shares we can buy is capped to prevent tail-risk concentration
            max_shares = (0.0075 * bankroll) / max(0.01, q_exec[i])
            # strict depth cap
            max_shares = min(max_shares, depth_caps[i])
            bounds.append((0.0, max_shares))
            
    # Constraint: sum(q_i * x_i) <= event_bankroll_cap
    def max_spend_constraint(x):
        return event_bankroll_cap - np.sum(q_exec * x)
        
    constraints = [{'type': 'ineq', 'fun': max_spend_constraint}]
    
    # Solve
    res = minimize(
        expected_log_growth, 
        x0, 
        args=(p_adj, q_exec, bankroll),
        method='SLSQP',
        bounds=bounds,
        constraints=constraints,
        options={'ftol': 1e-6, 'disp': False}
    )
    
    if not res.success:
        logger.warning(f"Portfolio optimizer failed: {res.message}")
        return [0.0] * n_buckets
        
    # Round down to integer shares
    x_opt = np.floor(res.x)
    
    # ── Post-Rounding Validation ──
    total_cost_opt = np.sum(q_exec * x_opt)
    if total_cost_opt > event_bankroll_cap:
        logger.warning(f"ROUNDING_INVALIDATED_TRADE: Rounded cost {total_cost_opt} exceeds cap {event_bankroll_cap}")
        return [0.0] * n_buckets
        
    for i in range(n_buckets):
        if x_opt[i] > depth_caps[i]:
            logger.warning(f"ROUNDING_INVALIDATED_TRADE: Bucket {i} rounded size {x_opt[i]} exceeds depth {depth_caps[i]}")
            return [0.0] * n_buckets
            
    # Re-check log growth to ensure we still have positive EV after rounding
    # The expected_log_growth function returns negative expected log growth
    opt_log_growth = -expected_log_growth(x_opt, p_adj, q_exec, bankroll)
    no_trade_log_growth = np.log(bankroll)
    
    if opt_log_growth <= no_trade_log_growth:
        logger.warning(f"ROUNDING_INVALIDATED_TRADE: Rounded solution has non-positive log growth ({opt_log_growth} <= {no_trade_log_growth})")
        return [0.0] * n_buckets
        
    logger.info(
        f"OPTIMIZER_REPORT:\n"
        f"  raw_optimizer_solution: {res.x}\n"
        f"  rounded_solution: {x_opt}\n"
        f"  total_cost: {total_cost_opt:.2f}\n"
        f"  worst_case_wealth: {min(bankroll - total_cost_opt + x_opt):.2f}\n"
        f"  expected_log_growth_before_trade: {no_trade_log_growth:.6f}\n"
        f"  expected_log_growth_after_trade: {opt_log_growth:.6f}\n"
        f"  delta_expected_log_growth: {(opt_log_growth - no_trade_log_growth):.6f}\n"
        f"  min_bucket_net_edge: {min(net_edges):.4f}\n"
        f"  max_bucket_net_edge: {max(net_edges):.4f}\n"
        f"  depth_caps: {depth_caps}"
    )
    
    return x_opt.tolist()
