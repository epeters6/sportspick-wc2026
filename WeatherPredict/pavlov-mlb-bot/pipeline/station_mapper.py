"""
pipeline/station_mapper.py – Maps Kalshi weather market titles to NWS
station metadata.

STATION_MAP: hardcoded dict of city → {station, lat, lon, nws_office,
             grid_x, grid_y}

get_city_for_market(market_title) -> str | None
    Fuzzy-match a city name from the market title string. Returns the
    matching STATION_MAP key, or None if no city is recognised.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Station map
# ---------------------------------------------------------------------------

STATION_MAP: dict[str, dict] = {
    "Washington DC": {
        "station":    "KDCA",
        "lat":         38.8521,
        "lon":        -77.0370,
        "nws_office": "LWX",
        "grid_x":      96,
        "grid_y":      70,
    },
    "San Francisco": {
        "station":    "KSFO",
        "lat":         37.6213,
        "lon":        -122.379,
        "nws_office": "MTR",
        "grid_x":      85,
        "grid_y":     105,
    },
    "Las Vegas": {
        "station":    "KLAS",
        "lat":         36.0840,
        "lon":        -115.153,
        "nws_office": "VEF",
        "grid_x":     122,
        "grid_y":      94,
    },
    "Chicago": {
        "station":    "KORD",
        "lat":         41.9742,
        "lon":        -87.9073,
        "nws_office": "LOT",
        "grid_x":      74,
        "grid_y":      74,
    },
    "Minneapolis": {
        "station":    "KMSP",
        "lat":         44.8848,
        "lon":        -93.2223,
        "nws_office": "MPX",
        "grid_x":      97,
        "grid_y":      72,
    },
    "New York": {
        "station":    "KJFK",
        "lat":         40.6413,
        "lon":        -73.7781,
        "nws_office": "OKX",
        "grid_x":      42,
        "grid_y":      40,
    },
    "Los Angeles": {
        "station":    "KLAX",
        "lat":         33.9425,
        "lon":        -118.408,
        "nws_office": "LOX",
        "grid_x":     155,
        "grid_y":      45,
    },
    "Miami": {
        "station":    "KMIA",
        "lat":         25.7959,
        "lon":        -80.2870,
        "nws_office": "MFL",
        "grid_x":     106,
        "grid_y":      51,
    },
    "Dallas": {
        "station":    "KDFW",
        "lat":         32.8998,
        "lon":        -97.0403,
        "nws_office": "FWD",
        "grid_x":      83,
        "grid_y":      64,
    },
    "Seattle": {
        "station":    "KSEA",
        "lat":         47.4502,
        "lon":        -122.308,
        "nws_office": "SEW",
        "grid_x":     124,
        "grid_y":      69,
    },
    "Denver": {
        "station":    "KDEN",
        "lat":         39.8561,
        "lon":        -104.673,
        "nws_office": "BOU",
        "grid_x":      57,
        "grid_y":      63,
    },
    "Phoenix": {
        "station":    "KPHX",
        "lat":         33.4373,
        "lon":        -112.007,
        "nws_office": "PSR",
        "grid_x":     157,
        "grid_y":      58,
    },
    "Boston": {
        "station":    "KBOS",
        "lat":         42.3656,
        "lon":        -71.0096,
        "nws_office": "BOX",
        "grid_x":      69,
        "grid_y":      81,
    },
    "Atlanta": {
        "station":    "KATL",
        "lat":         33.6407,
        "lon":        -84.4277,
        "nws_office": "FFC",
        "grid_x":      51,
        "grid_y":      88,
    },
    "Philadelphia": {
        "station":    "KPHL",
        "lat":         39.8719,
        "lon":        -75.2411,
        "nws_office": "PHI",
        "grid_x":      48,
        "grid_y":      75,
    },
    "Austin": {
        "station":    "KAUS",
        "lat":         30.1975,
        "lon":        -97.6664,
        "nws_office": "EWX",
        "grid_x":     159,
        "grid_y":      88,
    },
    "Houston": {
        "station":    "KIAH",
        "lat":         29.9902,
        "lon":        -95.3368,
        "nws_office": "HGX",
        "grid_x":      64,
        "grid_y":     105,
    },
    "San Antonio": {
        "station":    "KSAT",
        "lat":         29.5337,
        "lon":        -98.4698,
        "nws_office": "EWX",
        "grid_x":     127,
        "grid_y":      59,
    },
    "Oklahoma City": {
        "station":    "KOKC",
        "lat":         35.3931,
        "lon":        -97.6011,
        "nws_office": "OUN",
        "grid_x":      94,
        "grid_y":      90,
    },
}

# ---------------------------------------------------------------------------
# Alias table – common alternate names / abbreviations in market titles
# that don't literally contain the canonical city name.
# ---------------------------------------------------------------------------
_ALIASES: dict[str, str] = {
    "nyc":          "New York",
    "new york city":"New York",
    "brooklyn":     "New York",
    "manhattan":    "New York",
    "lax":          "Los Angeles",
    "l.a.":         "Los Angeles",
    "la":           "Los Angeles",
    "sfb":          "San Francisco",
    "sf":           "San Francisco",
    "bay area":     "San Francisco",
    "dc":           "Washington DC",
    "washington":   "Washington DC",
    "d.c.":         "Washington DC",
    "dca":          "Washington DC",
    "dfw":          "Dallas",
    "fort worth":   "Dallas",
    "msp":          "Minneapolis",
    "twin cities":  "Minneapolis",
    "chi":          "Chicago",
    "ord":          "Chicago",
    "sea":          "Seattle",
    "den":          "Denver",
    "phx":          "Phoenix",
    "bos":          "Boston",
    "atl":          "Atlanta",
    "mia":          "Miami",
    "las":          "Las Vegas",
    "vegas":        "Las Vegas",
    "phl":          "Philadelphia",
    "philly":       "Philadelphia",
    "aus":          "Austin",
    "hou":          "Houston",
    "iah":          "Houston",
    "sat":          "San Antonio",
    "satx":         "San Antonio",
    "san antonio":  "San Antonio",
    "okc":          "Oklahoma City",
    "oklahoma":     "Oklahoma City",
}

# Pre-build a sorted list of (canonical_lower, canonical_key) for substring
# matching. Sort longest first so "Los Angeles" beats "Los".
_CANONICAL_PAIRS: list[tuple[str, str]] = sorted(
    [(k.lower(), k) for k in STATION_MAP],
    key=lambda t: len(t[0]),
    reverse=True,
)


def _normalise(text: str) -> str:
    """Lower-case and collapse whitespace/punctuation for comparison."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_city_for_market(market_title: str) -> str | None:
    """Fuzzy-match a city name from a Kalshi market title.

    Matching strategy (in order):
    1. Check each alias token against the normalised title.
    2. Check each canonical city name as a substring of the normalised title.

    Returns the matching STATION_MAP key (e.g. "New York"), or None.

    Examples:
        "Will NYC reach 90°F?"          → "New York"
        "High Temp in Chicago tomorrow" → "Chicago"
        "Las Vegas High above 100"      → "Las Vegas"
    """
    norm = _normalise(market_title)

    # 1. Alias pass – check every alias token against the normalised title.
    for alias, canonical in _ALIASES.items():
        # Use word-boundary matching so "la" doesn't fire inside "dallas".
        pattern = r"\b" + re.escape(alias) + r"\b"
        if re.search(pattern, norm):
            return canonical

    # 2. Canonical substring pass (longest match first).
    for city_lower, city_key in _CANONICAL_PAIRS:
        if city_lower in norm:
            return city_key

    return None


