import json, sys
sys.path.insert(0, '.')

from pipeline import kalshi_client as kc
from pipeline.station_mapper import STATION_MAP, get_city_for_market
from pipeline import nws_client, signal_engine

markets = kc.get_weather_markets()
print(f"Total markets fetched: {len(markets)}")

# 1. Show which cities parse out of market titles
cities_seen = {}
for m in markets:
    city = get_city_for_market(m.get("title", ""))
    if city not in cities_seen:
        cities_seen[city] = m.get("title", "")

print("\n--- City parsing results ---")
for city, title in sorted(cities_seen.items(), key=lambda x: str(x[0])):
    in_map = city in STATION_MAP if city else False
    status = "OK     " if in_map else "MISSING"
    print(f"  {status} | {str(city):<25} | {title}")

# 2. For each OK city, check if NWS returns data
print("\n--- NWS data check ---")
import datetime
today = datetime.datetime.now().strftime("%Y-%m-%d")
for city in sorted(STATION_MAP.keys()):
    try:
        val = nws_client.get_predicted_high(city, today)
        print(f"  OK     | {city:<20} | high={val['predicted_high_f']}°F")
    except Exception as e:
        print(f"  FAIL   | {city:<20} | {e}")
