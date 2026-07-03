"""Probe Kalshi for weather markets using events and series endpoints."""
from pipeline.kalshi_client import _get

# 1. Try the events endpoint with category/search filter
print("=== /events (first 50) ===")
try:
    data = _get('/events', {'status': 'open', 'limit': 50})
    events = data.get('events', [])
    print(f"Total events returned: {len(events)}")
    for e in events:
        title = e.get('title', '')
        ticker = e.get('event_ticker', e.get('ticker', ''))
        cat = e.get('category', e.get('sub_title', ''))
        if any(w in title.lower() for w in ['temp', 'high', 'low', 'weather', 'degree']):
            print(f"  WEATHER [{ticker}] {title[:80]}  cat={cat}")
    print("  (first 10 events regardless):")
    for e in events[:10]:
        print(f"    {e.get('event_ticker', e.get('ticker',''))}: {e.get('title','')[:70]}")
except Exception as ex:
    print(f"  /events error: {ex}")

# 2. Try paginating markets - page 2 and 3
print()
print("=== /markets pages 2-3 ===")
try:
    cursor = None
    page = 0
    found_weather = []
    while page < 5:
        params = {'status': 'open', 'limit': 200}
        if cursor:
            params['cursor'] = cursor
        data = _get('/markets', params)
        markets = data.get('markets', [])
        cursor = data.get('cursor')
        page += 1
        weather = [m for m in markets if any(
            w in m.get('title','').lower()
            for w in ['temp', 'high', 'low', 'weather', 'degree', 'heat', 'cold']
        )]
        found_weather.extend(weather)
        print(f"  Page {page}: {len(markets)} markets, {len(weather)} weather, cursor={'yes' if cursor else 'no'}")
        if not cursor or len(markets) == 0:
            break
    print(f"\nWeather markets found across {page} pages: {len(found_weather)}")
    for m in found_weather[:20]:
        print(f"  [{m.get('close_time','')[:10]}]  {m.get('title','')[:80]}")
except Exception as ex:
    print(f"  Pagination error: {ex}")

# 3. Try known weather series tickers
print()
print("=== Known weather series tickers ===")
series_tickers = [
    'KXHIGHNY', 'KXLOWNY', 'KXHIGHDC', 'KXLOWDC',
    'KXHIGHCHI', 'KXLOWCHI', 'KXHIGHLA', 'KXLOWLA',
    'HIGHNY', 'LOWNY', 'HIGHDC', 'LOWDC',
]
for st in series_tickers:
    try:
        data = _get('/markets', {'status': 'open', 'series_ticker': st, 'limit': 5})
        markets = data.get('markets', [])
        if markets:
            print(f"  {st}: {len(markets)} markets — {markets[0].get('title','')[:60]}")
    except Exception as ex:
        print(f"  {st}: error {ex}")
