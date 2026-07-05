import os
import sys
import logging
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.db import get_db

logger = logging.getLogger(__name__)

def run_backtest(table: str = "autobets"):
    """
    Evaluates historical bets against their Closing Line Value (CLV)
    instead of their actual binary outcome. This isolates the bot's 
    predictive edge from standard variance.
    """
    db = get_db()
    
    # Fetch all bets with a closing price
    bets = db.table(table).select("*").not_.is_("closing_price", "null").execute().data or []
    
    if not bets:
        print(f"No bets found with CLV data in {table}.")
        return
        
    total_bets = len(bets)
    total_staked = sum(b.get("stake_size", 0.0) for b in bets)
    
    expected_profit = 0.0
    actual_profit = 0.0
    
    clv_beats = 0
    
    for b in bets:
        stake = b.get("stake_size", 0.0)
        placed_price = b.get("placed_price", 0.0)
        closing_price = b.get("closing_price", 0.0)
        
        if placed_price <= 0 or closing_price <= 0:
            continue
            
        # Expected value is calculated by assuming the closing price represents true probability
        # Payout is Stake / PlacedPrice. EV = (Payout * TrueProb) - Stake
        payout = stake / placed_price
        true_prob = closing_price
        
        ev = (payout * true_prob) - stake
        expected_profit += ev
        
        if closing_price > placed_price:
            clv_beats += 1
            
        # Calculate actual profit for comparison
        status = b.get("status", "")
        if status == "won":
            actual_profit += (payout - stake)
        elif status == "lost":
            actual_profit -= stake

    clv_beat_pct = (clv_beats / total_bets) * 100 if total_bets > 0 else 0
    expected_roi = (expected_profit / total_staked) * 100 if total_staked > 0 else 0
    actual_roi = (actual_profit / total_staked) * 100 if total_staked > 0 else 0
    
    print("\n" + "="*40)
    print(" HIGH-FIDELITY CLV BACKTEST REPORT ")
    print("="*40)
    print(f"Table             : {table}")
    print(f"Total Bets        : {total_bets}")
    print(f"Total Volume      : ${total_staked:,.2f}")
    print("-" * 40)
    print(f"CLV Beat Rate     : {clv_beat_pct:.1f}%")
    print(f"Expected Profit   : ${expected_profit:,.2f}")
    print(f"Expected ROI      : {expected_roi:+.2f}%")
    print("-" * 40)
    print(f"Actual Profit     : ${actual_profit:,.2f}")
    print(f"Actual ROI        : {actual_roi:+.2f}%")
    print("="*40)
    
    # Analyze difference to detect if we're running bad (variance) or if models are flawed
    diff = actual_roi - expected_roi
    if diff < -5.0:
        print("💡 INSIGHT: You are running worse than your closing edge (Negative Variance). Do not panic-adjust models.")
    elif diff > 5.0:
        print("💡 INSIGHT: You are running hot! Actual ROI exceeds expected ROI (Positive Variance).")
    elif expected_roi < -1.0:
        print("💡 INSIGHT: Expected ROI is negative. The models are not beating the market spread.")
    else:
        print("💡 INSIGHT: System is performing within expected mathematical bounds.")
    print("\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run High-Fidelity CLV Backtest")
    parser.add_argument("--table", type=str, default="autobets", help="Table to backtest (autobets or simulated_bets)")
    args = parser.parse_args()
    
    run_backtest(table=args.table)
