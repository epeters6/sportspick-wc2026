import numpy as np
from scipy.optimize import minimize
from typing import List
from loguru import logger

def expected_log_growth(x: np.ndarray, p_adj: np.ndarray, q_exec: np.ndarray, bankroll: float) -> float:
    """
    Negative expected log-wealth (minimize = maximize growth).

    x: shares bought per bucket (each share pays $1 if that outcome wins)
    q_exec: execution cost per share → dollars spent = sum(q_exec * x)
    Relative wealth if j wins: 1 - f + x_j/bankroll where f = dollars/bankroll,
    matching dollar-fraction Kelly (binary: 1 - f + f/c).
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
    p_adj = np.array(P_adj, dtype=float)
    q_exec = np.array(Q_exec, dtype=float)
    n_buckets = len(p_adj)
    net_edges = p_adj - q_exec
    event_bankroll_cap = 0.02 * bankroll

    bounds = []
    max_shares_list = []
    for i in range(n_buckets):
        if net_edges[i] < min_net_edge or q_exec[i] >= 1.0 or depth_caps[i] <= 0:
            bounds.append((0.0, 0.0))
            max_shares_list.append(0.0)
        else:
            max_shares = (0.0075 * bankroll) / max(0.01, q_exec[i])
            max_shares = min(max_shares, float(depth_caps[i]))
            bounds.append((0.0, max_shares))
            max_shares_list.append(max_shares)

    # Seed away from the all-zero saddle — SLSQP often "succeeds" at x=0 with large B.
    x0 = np.zeros(n_buckets)
    positive = [i for i in range(n_buckets) if max_shares_list[i] > 0]
    if positive:
        edges_pos = np.array([max(float(net_edges[i]), 0.0) for i in positive])
        if edges_pos.sum() <= 0:
            edges_pos = np.ones(len(positive))
        weights = edges_pos / edges_pos.sum()
        seed_budget = 0.5 * event_bankroll_cap
        for w, i in zip(weights, positive):
            if q_exec[i] <= 0:
                continue
            x0[i] = min(max_shares_list[i], (seed_budget * w) / q_exec[i])

    def max_spend_constraint(x):
        return event_bankroll_cap - np.sum(q_exec * x)

    res = minimize(
        expected_log_growth,
        x0,
        args=(p_adj, q_exec, bankroll),
        method="SLSQP",
        bounds=bounds,
        constraints=[{"type": "ineq", "fun": max_spend_constraint}],
        options={"ftol": 1e-12, "maxiter": 500, "disp": False},
    )

    raw_x = res.x if res.success else x0
    if not res.success:
        logger.warning(f"Portfolio optimizer failed: {res.message} — trying seeded fallback")

    def _score(x_vec: np.ndarray) -> float:
        return -expected_log_growth(x_vec, p_adj, q_exec, bankroll) - float(np.log(bankroll))

    def _feasible(x_vec: np.ndarray) -> bool:
        if float(np.sum(q_exec * x_vec)) > event_bankroll_cap + 1e-9:
            return False
        for i in range(n_buckets):
            if x_vec[i] > float(depth_caps[i]) + 1e-9:
                return False
            if x_vec[i] < -1e-9:
                return False
        return True

    candidates = [
        np.floor(raw_x),
        np.floor(x0),
    ]
    # Single-bucket greedy on best edge as last resort
    if n_buckets:
        best_i = int(np.argmax(net_edges))
        greedy = np.zeros(n_buckets)
        if max_shares_list[best_i] >= 1 and q_exec[best_i] > 0:
            greedy[best_i] = np.floor(
                min(max_shares_list[best_i], event_bankroll_cap / q_exec[best_i])
            )
        candidates.append(greedy)

    best_x = np.zeros(n_buckets)
    best_delta = 0.0
    for cand in candidates:
        if not _feasible(cand):
            continue
        delta = _score(cand)
        if delta > best_delta:
            best_delta = delta
            best_x = cand

    if best_delta <= 0:
        logger.warning(
            f"ROUNDING_INVALIDATED_TRADE: No integer portfolio improved log growth "
            f"(best_delta={best_delta:.6g}, max_edge={float(np.max(net_edges)):.4f})"
        )
        x_opt = np.zeros(n_buckets)
    else:
        x_opt = best_x

    total_cost_opt = float(np.sum(q_exec * x_opt))
    opt_log_growth = -expected_log_growth(x_opt, p_adj, q_exec, bankroll)
    no_trade_log_growth = float(np.log(bankroll))
    logger.info(
        f"OPTIMIZER_REPORT:\n"
        f"  raw_optimizer_solution: {raw_x}\n"
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
