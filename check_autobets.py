from backend.db import get_db
db = get_db()

res = db.table('autobets').select('id, model_prob').eq('bet_type', 'weather').eq('status', 'open').execute().data
bad_count = sum(1 for b in res if abs(b.get('model_prob', 0) - 0.08) < 0.0001)

print(f"Total open: {len(res)}, Exactly 8%: {bad_count}")

# Clean up the 8% ones
deleted = 0
for b in res:
    if abs(b.get('model_prob', 0) - 0.08) < 0.0001:
        db.table('autobets').delete().eq('id', b['id']).execute()
        deleted += 1

print(f"Deleted {deleted} bad bets.")
