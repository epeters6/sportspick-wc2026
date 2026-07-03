"""Find all active Kalshi weather series tickers for our 14 cities."""
from pipeline.kalshi_client import _get
import time

# Candidate series tickers to probe
CANDIDATES = [
    # New York
    'KXHIGHNY', 'KXLOWNY',
    # Chicago
    'KXHIGHCHI', 'KXLOWCHI',
    # Washington DC
    'KXHIGHDC', 'KXLOWDC', 'KXHIGHWDC', 'KXLOWWDC',
    # Los Angeles
    'KXHIGHLA', 'KXLOWLA', 'KXHIGHLOS', 'KXLOWLOS',
    # Miami
    'KXHIGHMIA', 'KXLOWMIA',
    # Dallas
    'KXHIGHDAL', 'KXLOWDAL', 'KXHIGHDFW', 'KXLOWDFW',
    # Las Vegas
    'KXHIGHLV', 'KXLOWLV', 'KXHIGHLAS', 'KXLOWLAS',
    # Minneapolis
    'KXHIGHMIN', 'KXLOWMIN', 'KXHIGHMSP', 'KXLOWMSP',
    # San Francisco
    'KXHIGHSF', 'KXLOWSF', 'KXHIGHSFO', 'KXLOWSFO',
    # Atlanta
    'KXHIGHATL', 'KXLOWATL',
    # Boston
    'KXHIGHBOS', 'KXLOWBOS',
    # Denver
    'KXHIGHDEN', 'KXLOWDEN', 'KXHIGHdenver', 'KXLOWDENVER',
    # Seattle
    'KXHIGHSEA', 'KXLOWSEA', 'KXHIGHSEAT', 'KXLOWSEAT',
    # Phoenix
    'KXHIGHPHX', 'KXLOWPHX', 'KXHIGHPHO', 'KXLOWPHO',
]

print(f"Probing {len(CANDIDATES)} series tickers...\n")
found = {}
for ticker in CANDIDATES:
    try:
        time.sleep(1.1)  # respect rate limit
        data = _get('/markets', {'status': 'open', 'series_ticker': ticker, 'limit': 3})
        markets = data.get('markets', [])
        if markets:
            title = markets[0].get('title', '')
            found[ticker] = title
            print(f"  OK  {ticker:<18} -> {title[:70]}")
        else:
            print(f"  --  {ticker:<18} (no open markets)")
    except Exception as ex:
        print(f"  ERR {ticker:<18} error: {ex}")

print(f"\n=== FOUND {len(found)} active series ===")
for k, v in found.items():
    print(f"  '{k}': '{v[:50]}'")
