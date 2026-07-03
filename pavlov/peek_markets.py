from pipeline.kalshi_client import _get

data = _get('/markets', {'status': 'open', 'limit': 200})
markets = data.get('markets', [])
print(f'Total markets: {len(markets)}')
print()

weather_words = ['high', 'low', 'temp', 'weather', 'rain', 'snow', 'wind',
                 'degree', 'warm', 'cold', 'heat', 'frost', 'freeze', 'humid']

print('=== WEATHER-LOOKING TITLES ===')
found = 0
for m in markets:
    title = m.get('title', '')
    close = m.get('close_time', '')[:10]
    if any(w in title.lower() for w in weather_words):
        print(f"  [{close}]  {title[:90]}")
        found += 1
if found == 0:
    print('  (none found)')

print()
print('=== ALL MARKET TITLES (first 60) ===')
for m in markets[:60]:
    title = m.get('title', '')
    close = m.get('close_time', '')[:10]
    print(f"  [{close}]  {title[:90]}")
