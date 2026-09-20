"""Versioned settlement registry; city names never establish settlement identity."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pavlov.pipeline.station_mapper import STATION_MAP, get_station_metadata
from pavlov.pipeline.settlement_resolver import derive_market_date
from .dataset import digest, finite, utc

REGISTRY_VERSION = "station_rules_2026_09_v3"
# These are candidates, not authorization to use a station without rule evidence.
CLI_CODES = {"CLINYC": "KNYC", "CLIMDW": "KMDW", "CLIORD": "KORD", "CLIMIA": "KMIA",
             "CLILAX": "KLAX", "CLISFO": "KSFO", "CLIDEN": "KDEN", "CLIPHL": "KPHL",
             "CLIAUS": "KAUS", "CLIDCA": "KDCA", "CLIPHX": "KPHX", "CLIDFW": "KDFW",
             "CLIIAH": "KIAH", "CLIMSP": "KMSP", "CLISEA": "KSEA", "CLILAS": "KLAS",
             "CLIBOS": "KBOS", "CLIOKC": "KOKC", "CLIATL": "KATL", "CLISAT": "KSAT", "CLIHOU": "KHOU"}
NEIGHBORS = {"KNYC": ["KLGA", "KEWR"], "KMDW": ["KORD", "KGYY"], "KORD": ["KMDW", "KDPA"],
             "KMIA": ["KFLL", "KTMB"], "KLAX": ["KSMO", "KLGB"], "KSFO": ["KOAK", "KSQL"],
             "KDEN": ["KAPA", "KBJC"], "KPHL": ["KPNE", "KILG"], "KAUS": ["KEDC", "KBAZ"],
             "KDCA": ["KIAD", "KBWI"], "KPHX": ["KSDL", "KIWA"], "KDFW": ["KDAL", "KFTW"],
             "KIAH": ["KHOU", "KDWH"], "KMSP": ["KSTP", "KFCM"], "KSEA": ["KBFI", "KRNT"],
             "KLAS": ["KVGT", "KHND"], "KBOS": ["KBED", "KOWD"], "KOKC": ["KPWA", "KOUN"],
             "KATL": ["KPDK", "KFTY"], "KSAT": ["KSSF", "KBAZ"], "KHOU": ["KIAH", "KSGR"]}
AIRPORT_NAMES = {
    "central park": "KNYC", "midway": "KMDW", "o'hare": "KORD", "ohare": "KORD",
    "miami international": "KMIA", "los angeles international": "KLAX", "san francisco international": "KSFO",
    "denver international": "KDEN", "philadelphia international": "KPHL", "austin-bergstrom": "KAUS",
    "reagan national": "KDCA", "phoenix sky harbor": "KPHX", "dallas/fort worth": "KDFW",
    "george bush intercontinental": "KIAH", "minneapolis-st paul": "KMSP", "seattle-tacoma": "KSEA",
    "harry reid international": "KLAS", "logan international": "KBOS", "will rogers": "KOKC",
    "hartsfield-jackson": "KATL", "san antonio international": "KSAT", "hobby airport": "KHOU",
}


def station_metadata(station):
    if station == "KHOU":
        # NWS CLIHOU is Houston/Hobby, not Bush Intercontinental (KIAH).
        # https://forecast.weather.gov/product.php?site=HGX&issuedby=HOU&product=CLI
        # Collection verifies the actual coordinates/elevation through AWC.
        return {"station": "KHOU", "city": "Houston", "timezone": "America/Chicago"}
    verified = get_station_metadata(station)
    if verified:
        return verified
    from pavlov.pipeline.station_mapper import get_tz_for_city
    for city, row in STATION_MAP.items():
        if row["station"] == station:
            return {**row, "city": city, "timezone": get_tz_for_city(city)}
    raise ValueError("UNSUPPORTED_STATION")


def bucket_bounds(market, platform, rules):
    """Kalshi strikes and explicit rule operators define integer-F buckets."""
    if platform != "kalshi":
        # Polymarket US describes integer-F climate outcomes directly. Do not
        # infer inclusive operators or negative signs from abbreviated titles.
        number = r"(-?\d+(?:\.\d+)?)\s*°?\s*F\b"
        for expression, lower in ((r"less than or equal to\s+", False), (r"greater than or equal to\s+", True)):
            match = re.search(expression + number, rules, re.I)
            if match:
                value = float(match.group(1))
                if not value.is_integer():
                    raise ValueError("UNVERIFIED_SETTLEMENT_PRECISION")
                supplied = finite(market.get("floor_strike" if lower else "ceiling_strike"))
                if supplied != value:
                    raise ValueError("RULE_STRIKE_MISMATCH")
                return (value - .5, float("inf")) if lower else (float("-inf"), value + .5)
        match = re.search(r"between\s+" + number + r"\s+and\s+" + number, rules, re.I)
        if match:
            lo, hi = map(float, match.groups())
            if not lo.is_integer() or not hi.is_integer() or lo > hi:
                raise ValueError("UNVERIFIED_SETTLEMENT_PRECISION")
            if lo != finite(market.get("threshold_lo")) or hi != finite(market.get("threshold_hi")):
                raise ValueError("RULE_STRIKE_MISMATCH")
            return lo - .5, hi + .5
        raise ValueError("UNSUPPORTED_BUCKET_BOUNDARIES")
    kind = market.get("strike_type")
    floor, cap = finite(market.get("floor_strike")), finite(market.get("cap_strike"))
    if any(v is not None and not v.is_integer() for v in (floor, cap)):
        raise ValueError("UNVERIFIED_SETTLEMENT_PRECISION")
    if kind == "between" and floor is not None and cap is not None and floor <= cap:
        return floor - .5, cap + .5
    if kind == "less" and cap is not None:
        match = re.search(r"less than\s+(-?\d+(?:\.\d+)?)", rules.lower())
        if not match or float(match.group(1)) != cap:
            raise ValueError("RULE_STRIKE_MISMATCH")
        return float("-inf"), cap - .5
    if kind == "greater" and floor is not None:
        match = re.search(r"greater than\s+(-?\d+(?:\.\d+)?)", rules.lower())
        if not match or float(match.group(1)) != floor:
            raise ValueError("RULE_STRIKE_MISMATCH")
        return floor + .5, float("inf")
    raise ValueError("UNSUPPORTED_BUCKET_BOUNDARIES")


@dataclass(frozen=True)
class Contract:
    market_id: str
    platform: str
    station: str
    target_date: str
    metric: str
    authority: str
    observation_timezone: str
    window_start: str
    window_end: str
    low_f: float | None
    high_f: float | None
    rules_text: str
    rules_hash: str
    registry_version: str = REGISTRY_VERSION
    precision_f: float = 1.0
    revision_policy: str = "venue_final_result_only"

    def record(self):
        return asdict(self)


def resolve(market: dict, platform: str) -> Contract:
    if platform not in ("kalshi", "polymarket"):
        raise ValueError("UNSUPPORTED_VENUE_PRODUCT")
    texts = [str(market.get(k) or "") for k in ("rules_primary", "rules_secondary", "description", "resolution_source")]
    rules = "\n".join(texts).strip()
    text = rules.lower()
    if not rules:
        raise ValueError("MISSING_SETTLEMENT_RULES")
    codes = set(re.findall(r"\b(?:K[A-Z0-9]{3}|CLI[A-Z]{3})\b", rules))
    candidates = {CLI_CODES.get(code, code) for code in codes}
    candidates.update(code for name, code in AIRPORT_NAMES.items() if name in text)
    explicit = market.get("settlement_station") or market.get("station_id")
    if explicit:
        candidates.add(CLI_CODES.get(str(explicit).upper(), str(explicit).upper()))
    if len(candidates) != 1:
        raise ValueError("MISSING_OR_CONFLICTING_SETTLEMENT_STATION")
    station = candidates.pop()
    if station not in set(CLI_CODES.values()):
        raise ValueError("UNSUPPORTED_SETTLEMENT_STATION")
    meta = station_metadata(station)
    if "the weather company" in text or "weather.com/kalshi" in text:
        authority = "The Weather Company"
    elif ("national weather service" in text or re.search(r"\bnws\b", text)) and ("climate" in text or "climatological" in text):
        authority = "NWS CLI"
    else:
        raise ValueError("UNSUPPORTED_SETTLEMENT_PRODUCT")
    if "fahrenheit" not in text and "°f" not in text and not re.search(r"-?\d+(?:\.\d+)?\s*F\b", rules, re.I):
        raise ValueError("UNVERIFIED_TEMPERATURE_UNIT")
    # Boilerplate secondary rules discuss both maximum and minimum markets.
    # The primary resolution clause establishes which metric this contract uses.
    metric_text = str(market.get("rules_primary") or market.get("description") or "").lower()
    high = any(word in metric_text for word in ("maximum temperature", "highest temperature", "high temperature"))
    low = any(word in metric_text for word in ("minimum temperature", "lowest temperature", "low temperature"))
    if high == low:
        raise ValueError("UNVERIFIED_DAILY_EXTREME")
    metric = "high" if high else "low"
    target = market.get("market_date")
    target = date.fromisoformat(target) if isinstance(target, str) else target
    target = target or derive_market_date(market, meta["city"])
    if not target:
        raise ValueError("MISSING_OBSERVATION_DATE")
    offset = datetime(2026, 1, 1, tzinfo=ZoneInfo(meta["timezone"])).utcoffset()
    hours = -int(offset.total_seconds() / 3600)
    zone = f"Etc/GMT{hours:+d}"
    start = datetime.combine(target, time(), ZoneInfo(zone))
    end = start + timedelta(days=1)
    # NWS CLI defines a standard-time reporting day. For TWC daily contracts,
    # require a CLI location reference plus a matching contract close; never
    # apply this convention to hourly contracts or foreign civil-day products.
    if authority == "The Weather Company":
        if platform != "kalshi" or not any(CLI_CODES.get(code) == station for code in codes):
            raise ValueError("UNVERIFIED_OBSERVATION_WINDOW")
        if utc(market.get("close_time")) != end.astimezone(timezone.utc):
            raise ValueError("CONFLICTING_OBSERVATION_WINDOW")
    lo, hi = bucket_bounds(market, platform, rules)
    import math
    return Contract(str(market.get("ticker") or market.get("id")), platform, station, target.isoformat(), metric,
                    authority, zone, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
                    lo if math.isfinite(lo) else None, hi if math.isfinite(hi) else None, rules, digest(rules))
