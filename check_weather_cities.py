import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "pavlov")))
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from polymarket import poly_client
poly_client.poly_configured = lambda: True
from polymarket_us import PolymarketUS
poly_client.get_client = lambda: PolymarketUS(key_id="dummy", secret_key="dummy")

markets = poly_client.get_weather_markets()
cities = set([m['city'] for m in markets if 'city' in m])
print(f"Total Weather Markets found: {len(markets)}")
print(f"Unique Cities found on Polymarket: {cities}")
