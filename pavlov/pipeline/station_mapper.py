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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

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
        "station":    "KNYC",
        "lat":         40.7812,
        "lon":        -73.9665,
        "nws_office": "OKX",
        "grid_x":      33,
        "grid_y":      36,
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

# IANA timezone per city — used for station-local lead-time / nowcast math.
_CITY_TZ: dict[str, str] = {
    "Washington DC": "America/New_York",
    "San Francisco": "America/Los_Angeles",
    "Las Vegas": "America/Los_Angeles",
    "Chicago": "America/Chicago",
    "Minneapolis": "America/Chicago",
    "New York": "America/New_York",
    "Los Angeles": "America/Los_Angeles",
    "Miami": "America/New_York",
    "Dallas": "America/Chicago",
    "Seattle": "America/Los_Angeles",
    "Denver": "America/Denver",
    "Phoenix": "America/Phoenix",
    "Boston": "America/New_York",
    "Atlanta": "America/New_York",
    "Philadelphia": "America/New_York",
    "Austin": "America/Chicago",
    "Houston": "America/Chicago",
    "San Antonio": "America/Chicago",
    "Oklahoma City": "America/Chicago",
}


def get_tz_for_city(city: str) -> str:
    """IANA timezone for a STATION_MAP city (defaults to US Eastern)."""
    return _CITY_TZ.get(city, "America/New_York")


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


# Versioned station registry for new research. Do not change STATION_MAP above:
# old evidence was produced with those city coordinates. These coordinates and
# timezones were checked against https://api.weather.gov/stations/{station}
# on 2026-09-12. In particular Chicago Midway and O'Hare are separate targets.
_VERIFIED_STATIONS: dict[str, dict] = {
    "KNYC": {"city": "New York", "lat": 40.78333, "lon": -73.96667,
             "timezone": "America/New_York", "cli": "CLINYC"},
    "KMDW": {"city": "Chicago", "lat": 41.78417, "lon": -87.75528,
             "timezone": "America/Chicago", "cli": "CLIMDW"},
    "KORD": {"city": "Chicago", "lat": 41.97972, "lon": -87.90444,
             "timezone": "America/Chicago"},
    "KMIA": {"city": "Miami", "lat": 25.79056, "lon": -80.31639,
             "timezone": "America/New_York", "cli": "CLIMIA"},
    "KLAX": {"city": "Los Angeles", "lat": 33.93806, "lon": -118.38889,
             "timezone": "America/Los_Angeles", "cli": "CLILAX"},
    "KSFO": {"city": "San Francisco", "lat": 37.61961, "lon": -122.36558,
             "timezone": "America/Los_Angeles", "cli": "CLISFO"},
    "KDEN": {"city": "Denver", "lat": 39.84658, "lon": -104.65622,
             "timezone": "America/Denver", "cli": "CLIDEN"},
    "KPHL": {"city": "Philadelphia", "lat": 39.87327, "lon": -75.22678,
             "timezone": "America/New_York", "cli": "CLIPHL"},
    "KAUS": {"city": "Austin", "lat": 30.18304, "lon": -97.67987,
             "timezone": "America/Chicago", "cli": "CLIAUS"},
}

# Actual open-contract rules checked on 2026-09-12, not inferred from a city:
# https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=...&status=open
# A series is a cross-check, never a substitute for absent contract metadata.
_VERIFIED_KALSHI_SERIES = {
    "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA",
    "KXHIGHLAX": "KLAX", "KXHIGHTSFO": "KSFO", "KXHIGHDEN": "KDEN",
    "KXHIGHPHIL": "KPHL", "KXHIGHAUS": "KAUS",
}
_CLI_STATIONS = {meta["cli"]: code for code, meta in _VERIFIED_STATIONS.items() if meta.get("cli")}
_STATION_NAMES = {
    "central park": "KNYC", "chicago midway airport": "KMDW",
    "chicago o'hare international airport": "KORD",
    "miami international airport": "KMIA",
    "los angeles international airport": "KLAX",
    "san francisco international airport": "KSFO",
    "denver international airport": "KDEN",
    "philadelphia international airport": "KPHL",
    "austin-bergstrom international airport": "KAUS",
}


class StationMappingError(ValueError):
    """A contract cannot safely be assigned to a supported settlement station."""


@dataclass(frozen=True)
class StationResolution:
    station: str
    city: str
    timezone: str
    settlement_source: str
    provenance: str


def get_station_metadata(station: str) -> dict | None:
    """Return a copy of vetted station metadata; never fall back by city."""
    code = str(station or "").strip().upper()
    meta = _VERIFIED_STATIONS.get(code)
    return {**meta, "station": code} if meta else None


def standard_timezone_for_station(station: str) -> str:
    """Fixed standard-time zone for the vetted US stations' CLI reporting day.

    NWS: https://www.weather.gov/lot/weather_observations_faq. Daily observations
    span midnight-to-midnight standard time, including during daylight saving.
    January 1 is standard time for every station in this US-only registry.
    """
    meta = get_station_metadata(station)
    if not meta:
        raise StationMappingError("UNSUPPORTED_SETTLEMENT_STATION")
    offset = datetime(2026, 1, 1, tzinfo=ZoneInfo(meta["timezone"])).utcoffset()
    if offset is None or offset.total_seconds() % 3600:
        raise StationMappingError("UNVERIFIED_OBSERVATION_DAY")
    hours = -int(offset.total_seconds() / 3600)  # IANA Etc/GMT signs are reversed.
    return f"Etc/GMT{hours:+d}" if hours else "Etc/GMT"


def resolve_observation_timezone(market: dict, platform: str,
                                 resolution: StationResolution, target: date) -> str:
    """Require a verified daily reporting convention, separately from city time.

    Kalshi daily-period guidance explicitly specifies standard time:
    https://help.kalshi.com/en/articles/13823837-weather-markets. Current vetted
    series refer to CLI stations but name The Weather Company; their actual
    close instant must also match the standard-day end. This does not relabel
    The Weather Company as NWS or infer the rule from a city name.
    """
    observation_tz = standard_timezone_for_station(resolution.station)
    if resolution.settlement_source == "NWS CLI":
        return observation_tz
    series = str(market.get("series_ticker") or market.get("ticker") or "").split("-")[0]
    if (platform != "kalshi" or series not in _VERIFIED_KALSHI_SERIES
            or resolution.settlement_source != "The Weather Company"):
        raise StationMappingError("UNVERIFIED_OBSERVATION_DAY")
    cli = _VERIFIED_STATIONS[resolution.station].get("cli")
    primary = str(market.get("rules_primary") or "")
    if not cli or not re.search(r"\b" + re.escape(cli) + r"\b", primary):
        raise StationMappingError("UNVERIFIED_OBSERVATION_DAY")
    try:
        actual_end = datetime.fromisoformat(str(market.get("close_time")).replace("Z", "+00:00"))
        expected_end = datetime.combine(target + timedelta(days=1), time(), ZoneInfo(observation_tz))
        if actual_end.tzinfo is None or actual_end.astimezone(timezone.utc) != expected_end.astimezone(timezone.utc):
            raise ValueError("wrong day end")
    except (TypeError, ValueError):
        raise StationMappingError("CONFLICTING_OBSERVATION_DAY") from None
    return observation_tz


def resolve_market_station(market: dict, platform: str) -> StationResolution:
    """Resolve explicit settlement metadata, rejecting conflicts and city guesses.

    Only settlement/rules fields are evidence. Titles, slugs and city hints do
    not establish a station. Supported Kalshi series constrain explicit rules
    as an additional check. Polymarket US is resolved independently; its public
    descriptions currently specify ICAO stations and NWS daily climate reports.
    """
    if platform not in {"kalshi", "polymarket", "poly_us", "polymarket_us"}:
        raise StationMappingError("UNSUPPORTED_WEATHER_VENUE")
    candidates: set[str] = set()
    evidence: set[str] = set()
    texts: list[str] = []
    containers = [market]
    if isinstance(market.get("metadata"), dict):
        containers.append(market["metadata"])
    for container in containers:
        for field in ("settlement_station", "settlementStation", "station_id", "weather_station"):
            raw = container.get(field)
            if raw is None or raw == "":
                continue
            code = str(raw).strip().upper()
            code = _CLI_STATIONS.get(code, code)
            if len(code) == 3:
                code = "K" + code
            if code not in _VERIFIED_STATIONS:
                raise StationMappingError("UNSUPPORTED_SETTLEMENT_STATION")
            candidates.add(code)
            evidence.add("structured_station")
        for field in ("rules_primary", "rules_secondary", "description", "resolution_source",
                      "resolutionSource", "settlement_source", "settlementSource"):
            raw = container.get(field)
            if isinstance(raw, str) and raw.strip():
                texts.append(raw)
    for text in texts:
        for token in re.findall(r"\b(?:K[A-Z0-9]{3}|CLI[A-Z]{3})\b", text):
            code = _CLI_STATIONS.get(token, token)
            if code not in _VERIFIED_STATIONS:
                raise StationMappingError("UNSUPPORTED_SETTLEMENT_STATION")
            candidates.add(code)
            evidence.add("contract_rules")
        for name, code in _STATION_NAMES.items():
            if name in text.lower():
                candidates.add(code)
                evidence.add("contract_rules")
    if not candidates:
        raise StationMappingError("MISSING_EXPLICIT_SETTLEMENT_STATION")
    if len(candidates) != 1:
        raise StationMappingError("CONFLICTING_SETTLEMENT_STATIONS")
    station = next(iter(candidates))
    meta = _VERIFIED_STATIONS[station]
    hinted_city = market.get("city_hint")
    if hinted_city and hinted_city != meta["city"]:
        raise StationMappingError("CONFLICTING_SETTLEMENT_CITY")
    if platform == "kalshi":
        series = str(market.get("series_ticker") or market.get("ticker") or "").split("-")[0]
        expected = _VERIFIED_KALSHI_SERIES.get(series)
        if expected and expected != station:
            raise StationMappingError("CONFLICTING_SERIES_SETTLEMENT_STATION")
        if expected:
            evidence.add("verified_kalshi_series")
    # Preserve the actual authority. Kalshi's current rules name The Weather
    # Company even though the location is expressed as a CLI station code.
    source_text = " ".join(texts).lower()
    if "the weather company" in source_text or "weather.com/kalshi" in source_text:
        source = "The Weather Company"
    elif "national weather service" in source_text or "nws" in source_text:
        source = "NWS CLI"
    else:
        source = "Venue official settlement"
    return StationResolution(station, meta["city"], meta["timezone"], source,
                             "+".join(sorted(evidence)))


