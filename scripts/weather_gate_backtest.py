from backend.db import get_db

def evaluate():
    db = get_db()
    rows = db.table('autobets').select(
        'id, question, outcome_name, market_prob, model_prob, edge, pnl, stake, status, created_at'
    ).eq('bet_type', 'weather').in_('status', ['won', 'lost']).execute().data or []
    
    all_bets = rows
    
    print(f"Total historical weather bets to evaluate: {len(all_bets)}")
    
    blocked = []
    survived = []
    
    for b in all_bets:
        q = b.get('question', '').lower()
        if 'above' in q:
            direction = 'above'
        elif 'below' in q:
            direction = 'below'
        elif 'in_range' in q or 'in range' in q:
            direction = 'in_range'
        else:
            # Default fallback
            direction = 'in_range'
            
        implied = b.get('market_prob') or 0.0
        model_p = b.get('model_prob') or 0.0
        edge_val = b.get('edge') or 0.0
        
        is_blocked = False
        reasons = []
        
        if direction == 'in_range' and implied < 0.05:
            is_blocked = True
            reasons.append('implied < 5%')
            
        if direction == 'in_range' and model_p < 0.15:
            is_blocked = True
            reasons.append('model < 15%')
            
        min_abs_edge = 0.10 if direction == 'in_range' else 0.06
        if abs(edge_val) < min_abs_edge:
            is_blocked = True
            reasons.append(f"edge < {min_abs_edge}")
            
        if is_blocked:
            b['reasons'] = ", ".join(reasons)
            blocked.append(b)
        else:
            survived.append(b)
            
    print(f"\n--- Backtest Results ---")
    print(f"Blocked: {len(blocked)} ({len(blocked)/len(all_bets)*100:.1f}%)")
    print(f"Survived: {len(survived)} ({len(survived)/len(all_bets)*100:.1f}%)")
    
    for name, group in [("Blocked", blocked), ("Survived", survived)]:
        staked = sum(b.get('stake') or 0 for b in group)
        pnl = sum(b.get('pnl') or 0 for b in group)
        roi = (pnl/staked*100) if staked else 0
        wins = sum(1 for b in group if b.get('status') == 'won')
        print(f"{name} Group: {len(group)} bets | {wins} won | Staked: ${staked:.2f} | PnL: ${pnl:.2f} | ROI: {roi:.1f}%")
        
    # Let's break down the reasons for blocked
    from collections import Counter
    reason_counts = Counter()
    for b in blocked:
        for r in b['reasons'].split(', '):
            reason_counts[r] += 1
            
    print("\nBlocked reasons breakdown:")
    for r, c in reason_counts.most_common():
        print(f"  {r}: {c} bets")

if __name__ == "__main__":
    evaluate()
