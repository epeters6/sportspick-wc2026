import sys
import os
import argparse
import numpy as np
import pandas as pd
from loguru import logger

# Add parent dir to path so we can import backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.db import get_db

def bootstrap_roi(pnl_array, n_iterations=1000, confidence=0.95):
    """Calculate bootstrapped confidence intervals for sum of PNL."""
    n_samples = len(pnl_array)
    if n_samples == 0:
        return 0, 0, 0
    
    # Resample with replacement
    indices = np.random.randint(0, n_samples, size=(n_iterations, n_samples))
    bootstrapped_pnls = pnl_array[indices]
    
    # Sum PNL for each bootstrap iteration
    sums = np.sum(bootstrapped_pnls, axis=1)
    
    # Calculate confidence intervals
    lower_percentile = ((1.0 - confidence) / 2.0) * 100
    upper_percentile = (confidence + ((1.0 - confidence) / 2.0)) * 100
    
    lower_bound = np.percentile(sums, lower_percentile)
    upper_bound = np.percentile(sums, upper_percentile)
    mean_pnl = np.mean(sums)
    
    return mean_pnl, lower_bound, upper_bound

def run_backtest(sport=None, n_iterations=10000, edge_buckets=4):
    db = get_db()
    
    query = db.table('autobets').select('sport, status, pnl, edge, mode, created_at, market_price, model_prob').in_('status', ['won', 'lost'])
    if sport:
        query = query.eq('sport', sport.lower())
        
    data = query.execute().data
    df = pd.DataFrame(data)
    
    if df.empty:
        logger.warning(f"No resolved bets found for sport: {sport}")
        return
        
    print(f"\n--- Bootstrapped ROI Analysis: {sport.upper() if sport else 'ALL SPORTS'} ---")
    print(f"Total resolved bets: {len(df)}")
    
    # Bucket by edge
    try:
        df['edge_bucket'] = pd.qcut(df['edge'], q=edge_buckets, duplicates='drop')
    except ValueError:
        logger.error("Not enough variance in edge to create quantiles. Using absolute bins.")
        df['edge_bucket'] = pd.cut(df['edge'], bins=[-np.inf, 0.015, 0.03, 0.05, np.inf])

    buckets = df['edge_bucket'].unique()
    buckets = sorted([b for b in buckets if pd.notna(b)])
    
    print("\n{:<25} | {:<10} | {:<10} | {:<20}".format("Edge Bucket", "N Bets", "Real PNL", "95% CI (Bootstrapped)"))
    print("-" * 75)
    
    for bucket in buckets:
        bucket_df = df[df['edge_bucket'] == bucket]
        pnl_array = bucket_df['pnl'].values
        
        real_pnl = np.sum(pnl_array)
        n_bets = len(pnl_array)
        
        if n_bets < 5:
            print("{:<25} | {:<10} | {:<10.2f} | (Insufficient Data)".format(str(bucket), n_bets, real_pnl))
            continue
            
        mean, lower, upper = bootstrap_roi(pnl_array, n_iterations=n_iterations)
        
        ci_str = f"[{lower:+.2f}, {upper:+.2f}]"
        
        # Determine significance
        sig = ""
        if lower > 0:
            sig = " (Sig. Positive)"
        elif upper < 0:
            sig = " (Sig. Negative)"
            
        print("{:<25} | {:<10} | {:<10.2f} | {:<20} {}".format(str(bucket), n_bets, real_pnl, ci_str, sig))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bootstrap backtest for edge buckets")
    parser.add_argument("--sport", type=str, default=None, help="Filter by sport (e.g. mlb, football)")
    parser.add_argument("--iters", type=int, default=10000, help="Number of bootstrap iterations")
    parser.add_argument("--buckets", type=int, default=4, help="Number of edge quantiles")
    args = parser.parse_args()
    
    if args.sport:
        run_backtest(args.sport, args.iters, args.buckets)
    else:
        run_backtest('football', args.iters, args.buckets)
        run_backtest('mlb', args.iters, args.buckets)
