import numpy as np
from backend.db import get_db
from collections import defaultdict

def bootstrap_roi(returns, n_iterations=10000, confidence_level=0.95):
    """Calculate bootstrap confidence interval for ROI."""
    if not returns:
        return 0.0, 0.0
    returns = np.array(returns)
    n = len(returns)
    means = []
    
    # Resample with replacement
    for _ in range(n_iterations):
        sample = np.random.choice(returns, size=n, replace=True)
        means.append(np.mean(sample))
        
    means = np.sort(means)
    lower_percentile = (1 - confidence_level) / 2 * 100
    upper_percentile = (1 + confidence_level) / 2 * 100
    
    lower_bound = np.percentile(means, lower_percentile)
    upper_bound = np.percentile(means, upper_percentile)
    
    return lower_bound * 100, upper_bound * 100

def evaluate_sports():
    db = get_db()
    # Fetch live bets for non-weather
    rows = db.table('autobets').select(
        'sport, status, stake, pnl, clv'
    ).neq('bet_type', 'weather').in_('status', ['won', 'lost']).execute().data or []
    
    by_sport = defaultdict(list)
    for r in rows:
        sport = r.get('sport') or 'unknown'
        by_sport[sport].append(r)
        
    for sport, bets in sorted(by_sport.items()):
        n = len(bets)
        wins = sum(1 for b in bets if b['status'] == 'won')
        staked = sum(b.get('stake') or 0 for b in bets)
        pnl = sum(b.get('pnl') or 0 for b in bets)
        roi = (pnl / staked * 100) if staked else 0.0
        
        # Calculate returns array for bootstrap (per-bet ROI)
        # Using simple PnL / Stake
        returns = [(b.get('pnl') or 0) / (b.get('stake') or 1.0) for b in bets if b.get('stake', 0) > 0]
        lower, upper = bootstrap_roi(returns)
        
        # CLV stats
        clvs = [b.get('clv') for b in bets if b.get('clv') is not None]
        if clvs:
            mean_clv = sum(clvs) / len(clvs)
            # Beating the close means CLV > 0
            beats = sum(1 for c in clvs if c > 0)
            beat_rate = beats / len(clvs) * 100
        else:
            mean_clv = 0.0
            beat_rate = 0.0
            
        print(f"--- {sport.upper()} ---")
        print(f"Sample Size: {n} bets ({wins} wins)")
        print(f"ROI: {roi:.1f}% [95% CI: {lower:.1f}% to {upper:.1f}%]")
        print(f"CLV: Mean = {mean_clv:+.4f} | Beating Close Rate: {beat_rate:.1f}% ({beats}/{len(clvs)} bets)")
        print()

if __name__ == "__main__":
    evaluate_sports()
