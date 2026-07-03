"""
Extended probe: test every plausible weather series ticker extracted from
the /series endpoint response, including the KXHIGHT*/KXLOWT* naming scheme.
"""
import time
from pipeline.kalshi_client import _get

CANDIDATES = [
    # --- Already confirmed active ---
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHDEN",
    "KXHIGHLAX", "KXHIGHPHIL", "KXHIGHAUS",

    # --- KXHIGHT* pattern (from /series dump) ---
    "KXHIGHTDC",   "KXHIGHTPHX", "KXHIGHTDAL", "KXHIGHTHOU",
    "KXHIGHTMIN",  "KXHIGHTSEA", "KXHIGHTLV",  "KXHIGHTBOS",
    "KXHIGHTOKC",  "KXHIGHTATL", "KXHIGHTSFO", "KXHIGHTSATX",
    "KXHIGHTPHIL", "KXHIGHTCHI", "KXHIGHTDEN", "KXHIGHTNY",
    "KXHIGHTMIA",  "KXHIGHTLAX", "KXHIGHTAUS",

    # --- Houston variants ---
    "KXHIGHOU", "KXHIGHHOU", "KXHOUHIGH", "KXHIGHTHOU",

    # --- KXLOWT* pattern ---
    "KXLOWTNYC",   "KXLOWTCHI",  "KXLOWTMIA",  "KXLOWTDEN",
    "KXLOWTDC",    "KXLOWTLAX",  "KXLOWTDAL",  "KXLOWTLV",
    "KXLOWTMIN",   "KXLOWTSFO",  "KXLOWTATL",  "KXLOWTBOS",
    "KXLOWTSEA",   "KXLOWTPHX",  "KXLOWTHOU",  "KXLOWTOKC",
    "KXLOWTSATX",  "KXLOWTPHIL", "KXLOWTAUS",

    # --- Original KXLOW* we already have ---
    "KXLOWNY",  "KXLOWCHI", "KXLOWMIA", "KXLOWDEN",
    "KXLOWDC",  "KXLOWLA",  "KXLOWDAL", "KXLOWLV",
    "KXLOWMIN", "KXLOWSF",  "KXLOWATL", "KXLOWBOS",
    "KXLOWSEA", "KXLOWPHX",

    # --- New city LOW variants ---
    "KXLOWPHIL", "KXLOWAUS", "KXLOWLAX", "KXLOWNYC",
    "KXLOWHOU",  "KXLOWTLAX",

    # --- No-KX prefix (seen in /series) ---
    "HIGHCHI", "HIGHMIA", "HIGHNY", "HIGHAUS", "HIGHNY0",
]

# Deduplicate while preserving order
seen = set()
CANDIDATES = [x for x in CANDIDATES if not (x in seen or seen.add(x))]

print(f"Probing {len(CANDIDATES)} tickers ...\n")
found = {}
for ticker in CANDIDATES:
    try:
        time.sleep(0.6)
        data = _get("/markets", {"status": "open", "series_ticker": ticker, "limit": 2})
        markets = data.get("markets", [])
        if markets:
            title = markets[0].get("title", "")
            found[ticker] = title
            print(f"  FOUND  {ticker:<22} -> {title[:65]}")
        # else silent
    except Exception as exc:
        print(f"  ERR    {ticker:<22} -> {exc}")

print(f"\n=== {len(found)} active series ===")
for k in sorted(found):
    print(f"  '{k}'")
