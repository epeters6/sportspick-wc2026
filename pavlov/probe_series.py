"""
Probe Kalshi's /series and /events endpoints to find all active weather series.
Also tries a broad grid of KXHIGH*/KXLOW* ticker patterns for major US cities.
"""
import time
from pipeline.kalshi_client import _get

WEATHER_KEYWORDS = ["temp", "high", "low", "weather", "degree", "warm", "cold", "heat"]

# ── 1. /series endpoint ────────────────────────────────────────────────────
print("=== /series endpoint ===")
try:
    data = _get("/series", {"limit": 200})
    all_series = data.get("series", [])
    print(f"Total series: {len(all_series)}")
    weather = [
        s for s in all_series
        if any(w in s.get("title", "").lower() for w in WEATHER_KEYWORDS)
    ]
    print(f"Weather-related: {len(weather)}")
    for s in weather:
        print(f"  {s.get('ticker', '?'):<25} | {s.get('title', '')[:70]}")
except Exception as exc:
    print(f"  ERROR: {exc}")

print()

# ── 2. /events endpoint ────────────────────────────────────────────────────
print("=== /events endpoint ===")
try:
    data = _get("/events", {"status": "open", "limit": 200})
    events = data.get("events", [])
    print(f"Total events: {len(events)}")
    weather = [
        e for e in events
        if any(w in e.get("title", "").lower() for w in WEATHER_KEYWORDS)
    ]
    print(f"Weather-related: {len(weather)}")
    for e in weather[:30]:
        print(f"  {e.get('event_ticker', '?'):<30} | {e.get('title', '')[:60]}")
except Exception as exc:
    print(f"  ERROR: {exc}")

print()

# ── 3. Broad KXHIGH*/KXLOW* ticker grid ───────────────────────────────────
print("=== Broad series ticker probe ===")
CITY_CODES = [
    # Already known working
    "NY", "CHI", "MIA", "DEN",
    # Alternatives for existing cities
    "DC", "WDC", "IAD", "DCA",
    "LA", "LAX", "LOS",
    "LV", "LAS", "VGS",
    "MIN", "MSP",
    "SF", "SFO", "SFB",
    "ATL",
    "BOS",
    "SEA", "SEAT",
    "PHX", "PHO",
    "DAL", "DFW",
    # New cities
    "HOU", "HOUS", "IAH",
    "PHL", "PHIL",
    "SAN", "SD",
    "PDX", "PORT",
    "SLC", "SALT",
    "STL", "SL",
    "DET", "DTW",
    "BNA", "NSH", "NASH",
    "CLE", "CLEV",
    "MKE", "MIL",
    "PIT", "PITT",
    "CIN", "CVG",
    "IND",
    "CMH", "COL",
    "MCI", "KC", "KCK",
    "CLT", "CHAR",
    "RDU", "RAL",
    "OKC",
    "TPA", "TAMP",
    "MSY", "NO", "NOLA",
    "SAT", "SAT",
    "AUS",
    "ABQ", "ALB",
    "TUS",
    "SMF", "SAC",
    "BUF",
    "JAX",
    "MEM",
    "LOU", "SDF",
    "RIC",
    "BWI", "BAL",
    "ORF",
]

found = {}
for code in CITY_CODES:
    for prefix in ("KXHIGH", "KXLOW"):
        ticker = f"{prefix}{code}"
        try:
            time.sleep(0.6)
            data = _get("/markets", {"status": "open", "series_ticker": ticker, "limit": 2})
            markets = data.get("markets", [])
            if markets:
                title = markets[0].get("title", "")
                found[ticker] = title
                print(f"  FOUND  {ticker:<20} -> {title[:65]}")
        except Exception:
            pass

print(f"\n=== SUMMARY: {len(found)} active series found ===")
for k, v in sorted(found.items()):
    print(f"  '{k}': '{v[:60]}'")
