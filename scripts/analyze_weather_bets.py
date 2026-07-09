from backend.db import get_db
from collections import Counter, defaultdict

db = get_db()
rows = db.table('autobets').select(
    'sport,status,stake,pnl,edge,market_price,mode,bet_type,question,outcome_name,created_at'
).neq('bet_type','weather').order('created_at',desc=True).limit(100).execute().data or []

print(f'Non-weather bets: {len(rows)}')
by_sport = defaultdict(list)
for r in rows:
    by_sport[r.get('sport') or 'unknown'].append(r)

for sport, bets in sorted(by_sport.items()):
    settled = [b for b in bets if b['status'] in ('won','lost')]
    won = [b for b in settled if b['status']=='won']
    open_ct = sum(1 for b in bets if b['status']=='open')
    staked = sum(b.get('stake') or 0 for b in settled)
    pnl = sum(b.get('pnl') or 0 for b in settled)
    roi = (pnl/staked*100) if staked else 0
    print(f'  {sport}: {len(bets)} total | {len(settled)} settled | {len(won)} won | open={open_ct} | staked=${staked:.2f} | pnl=${pnl:.2f} | roi={roi:.1f}%')

open_bets = [r for r in rows if r['status']=='open']
print(f'\nOpen non-weather bets: {len(open_bets)}')
for b in open_bets[:15]:
    print(f"  [{b.get('sport')}] {b.get('question','')[:60]} | stake=${b.get('stake')} | created={b.get('created_at','')[:10]}")

# Check for duplicate opens
by_q = defaultdict(list)
for b in open_bets:
    by_q[b.get('question','')[:70]].append(b)
dups = {k:v for k,v in by_q.items() if len(v)>1}
if dups:
    print(f'\nDuplicate open bets ({len(dups)} markets):')
    for k,v in dups.items():
        print(f"  {k}: {len(v)} copies")
else:
    print('No duplicate open bets found.')
