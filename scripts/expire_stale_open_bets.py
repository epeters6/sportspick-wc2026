from backend.db import get_db
from datetime import datetime, timezone

db = get_db()
open_bets = (
    db.table('autobets')
    .select('id,question,created_at,stake')
    .eq('status', 'open')
    .neq('bet_type', 'weather')
    .execute()
    .data or []
)
now = datetime.now(timezone.utc)
for b in open_bets:
    created = b.get('created_at') or ''
    if not created:
        continue
    try:
        dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
        age = (now - dt).days
        if age >= 6:
            q = str(b.get('question', ''))[:60]
            stake = b.get('stake') or 0
            print(f'Expiring (age={age}d stake=${stake}): {q}')
            db.table('autobets').update({
                'status': 'lost',
                'pnl': round(-stake, 2),
                'resolved_at': now.isoformat()
            }).eq('id', b['id']).execute()
    except Exception as e:
        print(f'Error: {e}')
print('Done')
