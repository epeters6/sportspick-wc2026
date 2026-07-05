import asyncio
from backend.db import get_db

async def main():
    db = get_db()
    # Get the 22 lost weather bets
    bets = db.table('autobets').select('id, question, market_price, closing_price').eq('sport', 'weather').eq('status', 'lost').execute().data
    
    print(f"Analyzing CLV drift for {len(bets)} lost weather bets:")
    
    total_drift = 0.0
    drifts = []
    
    for bet in bets:
        # If closing_price isn't recorded in the DB, it's None. We might not have it.
        # But wait! The database has 'clv' column.
        entry_price = bet.get('market_price')
        closing_price = bet.get('closing_price')
        
        if entry_price is not None and closing_price is not None:
            drift = entry_price - closing_price # positive means market moved against us (if we bet Yes)
            drifts.append(drift)
            print(f"{bet['question'][:40]:<40} | Entry: {entry_price:.3f} | Close: {closing_price:.3f} | Drift: {drift:+.3f}")
        else:
            # We don't have closing price recorded.
            pass
            
    if drifts:
        print(f"\nAverage CLV drift: {sum(drifts)/len(drifts):+.3f}")
    else:
        print("No closing_price data available for these bets in the database.")

if __name__ == "__main__":
    asyncio.run(main())
