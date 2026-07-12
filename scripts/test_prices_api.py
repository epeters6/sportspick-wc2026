import asyncio
import httpx
from backend.db import get_db

async def test_api():
    db = get_db()
    res = db.table('autobets').select('token_id, market_id').in_('status', ['won', 'lost']).limit(5).execute()
    
    for row in res.data:
        token = row.get('token_id') or row.get('market_id')
        if not token: continue
        
        url = f'https://clob.polymarket.com/prices-history?market={token}&interval=1m&fidelity=60'
        url_1min = f'https://clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=1'
        
        async with httpx.AsyncClient() as client:
            r1 = await client.get(url)
            data1 = r1.json().get('history', [])
            print(f'Token {token} -> 60min/1mo request returned {len(data1)} candles.')
            if data1:
                print(f"  First: {data1[0].get('t')} | Last: {data1[-1].get('t')}")
                
            r2 = await client.get(url_1min)
            data2 = r2.json().get('history', [])
            print(f'Token {token} -> 1min/max request returned {len(data2)} candles.')

asyncio.run(test_api())
