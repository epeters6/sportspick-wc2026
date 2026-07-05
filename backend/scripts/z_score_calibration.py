import asyncio
import json
from backend.db import get_db

async def main():
    db = get_db()
    
    # We query autobets because they have 'status' (won/lost) and 'model_prob'.
    bets = db.table('autobets').select('model_prob, market_price, status, raw_confidence, question').eq('sport', 'football').in_('status', ['won', 'lost']).execute().data
    
    print(f"Loaded {len(bets)} total settled football bets.")
    
    buckets = {
        "0-1 SD": {"expected_hits": 0.0, "actual_hits": 0, "count": 0},
        "1-2 SD": {"expected_hits": 0.0, "actual_hits": 0, "count": 0},
        "2-3 SD": {"expected_hits": 0.0, "actual_hits": 0, "count": 0},
        "3+ SD":  {"expected_hits": 0.0, "actual_hits": 0, "count": 0},
    }
    
    for bet in bets:
        prob = bet['model_prob']
        tail_prob = min(prob, 1 - prob)
        
        if tail_prob > 0.16:
            bucket = "0-1 SD"
        elif tail_prob > 0.067:
            bucket = "1-2 SD"
        elif tail_prob > 0.022:
            bucket = "2-3 SD"
        else:
            bucket = "3+ SD"
            
        b = buckets[bucket]
        b["count"] += 1
        b["expected_hits"] += prob
        
        actual = 1.0 if bet['status'] == 'won' else 0.0
        b["actual_hits"] += actual
        
    print("\nCalibration Audit by Tail Distance (Approximate Z-Score from Model Prob):")
    for b_name, b_data in buckets.items():
        count = b_data["count"]
        if count > 0:
            exp_rate = b_data["expected_hits"] / count
            act_rate = b_data["actual_hits"] / count
            ratio = act_rate / exp_rate if exp_rate > 0 else 0
            
            print(f"{b_name:<8} | Bets: {count:<4} | Exp Hit Rate: {exp_rate:.3f} | Act Hit Rate: {act_rate:.3f} | Realized Ratio: {ratio:.3f}x")
        else:
            print(f"{b_name:<8} | Bets: 0    | Exp Hit Rate: N/A   | Act Hit Rate: N/A")

if __name__ == "__main__":
    asyncio.run(main())
