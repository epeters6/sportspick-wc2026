"""Deep audit of weather bets - find all the bugs."""
from backend.db import get_db
from collections import Counter, defaultdict

db = get_db()
rows = db.table('autobets').select('*').eq('bet_type','weather').order('created_at').execute().data or []
print(f"Total weather bets in DB: {len(rows)}")

# 1. Check "in_range" vs reality: what is the Chicago July 6 win?
won = [r for r in rows if r['status'] == 'won']
print(f"\n=== WON BETS ({len(won)}) ===")
for r in won:
    mkt = r.get('market_id','')
    q = r.get('question','')
    stake = r.get('stake') or 0
    shares = r.get('shares') or 0
    price = r.get('market_price') or 0
    pnl = r.get('pnl') or 0
    print(f"  {mkt}")
    print(f"  q: {q}")
    print(f"  stake=${stake:.2f} market_price={price:.4f} shares={shares:.1f}")
    print(f"  pnl=${pnl:.2f} | ROI = {(pnl/stake*100) if stake else 0:.0f}%")

# 2. Still-open bets - what are they?
open_bets = [r for r in rows if r['status'] == 'open']
print(f"\n=== STILL OPEN ({len(open_bets)}) ===")
for r in open_bets:
    print(f"  {r.get('market_id')} stake=${r.get('stake'):.2f} created={r.get('created_at','')[:10]}")

# 3. Edge 7.0% cluster - what's going on?
seven_pct = [r for r in rows if r.get('edge') and abs(r['edge'] - 0.07) < 0.001]
print(f"\n=== 7.0% EDGE CLUSTER ({len(seven_pct)} bets, all lost) ===")
print("These look like a flood of identical Kelly-sized bets:")
for r in seven_pct:
    print(f"  {r.get('market_id')} model_prob={r.get('model_prob'):.3f} mkt_price={r.get('market_price'):.3f} stake=${r.get('stake'):.2f} created={r.get('created_at','')[:13]}")

# 4. Check model_prob vs implied_prob for potential calibration error
lost = [r for r in rows if r['status'] == 'lost']
print(f"\n=== LOST BET STATS ({len(lost)}) ===")
total_staked = sum(r.get('stake') or 0 for r in lost)
total_pnl = sum(r.get('pnl') or 0 for r in lost)
print(f"  Total staked: ${total_staked:.2f}")
print(f"  Total PnL: ${total_pnl:.2f}")
print(f"  If we exclude the Chicago anomaly: lost ROI = {(total_pnl/total_staked*100) if total_staked else 0:.1f}%")

# 5. Avg model_prob vs actual win rate
model_probs = [r.get('model_prob') or 0 for r in rows if r.get('model_prob')]
avg_model_prob = sum(model_probs)/len(model_probs) if model_probs else 0
actual_win_rate = len(won) / len(rows) if rows else 0
print(f"\n=== CALIBRATION CHECK ===")
print(f"  Avg model prob: {avg_model_prob:.1%}")
print(f"  Actual win rate: {actual_win_rate:.1%} ({len(won)}/{len(rows)})")
print(f"  Expected wins at avg prob: {avg_model_prob * len(rows):.1f}")

# 6. Show the market_price distribution (this tells us about the contracts being bet)
print(f"\n=== MARKET PRICE (implied prob) DISTRIBUTION ===")
price_ranges = Counter()
for r in rows:
    p = r.get('market_price') or 0
    if p < 0.05: price_ranges['<5%'] += 1
    elif p < 0.10: price_ranges['5-10%'] += 1
    elif p < 0.20: price_ranges['10-20%'] += 1
    elif p < 0.30: price_ranges['20-30%'] += 1
    else: price_ranges['>30%'] += 1
for k,v in sorted(price_ranges.items()):
    print(f"  {k}: {v} bets")
