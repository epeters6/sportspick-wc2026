"""Debug parse_market against live market titles and check NWS grid coords."""
import json
from pipeline.kalshi_client import _load_cache
from pipeline.signal_engine import parse_market
from pipeline.station_mapper import STATION_MAP, get_city_for_market
from pipeline import nws_client

# 1. Test parse_market on cached market titles
print("=== PARSE_MARKET TEST ===")
cache = _load_cache()
markets = cache.get('markets', [])
print(f"Cached markets: {len(markets)}")
print()

for m in markets[:15]:
    title = m.get('title', '')
    print(f"Title: {title}")
    city = get_city_for_market(title)
    print(f"  City match: {city}")
    result = parse_market(m)
    print(f"  Parse result: {result}")
    print()

# 2. Test NWS URLs for each active city
print("=== NWS URL TEST ===")
for city in ['New York', 'Chicago', 'Miami', 'Denver']:
    meta = STATION_MAP[city]
    office = meta['nws_office']
    gx = meta['grid_x']
    gy = meta['grid_y']
    url = f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}/forecast/hourly"
    print(f"{city}: {url}")
    import requests
    try:
        r = requests.get(url, headers={"User-Agent": "pavlov-bot/1.0"}, timeout=10)
        print(f"  Status: {r.status_code}")
        if r.status_code != 200:
            # Try the points API to get correct grid
            lat = meta['lat']
            lon = meta['lon']
            pr = requests.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers={"User-Agent": "pavlov-bot/1.0"},
                timeout=10
            )
            if pr.status_code == 200:
                props = pr.json().get('properties', {})
                print(f"  Correct office: {props.get('gridId')}")
                print(f"  Correct grid X: {props.get('gridX')}")
                print(f"  Correct grid Y: {props.get('gridY')}")
    except Exception as e:
        print(f"  Error: {e}")
    print()
