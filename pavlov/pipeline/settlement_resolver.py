from typing import Literal, Optional, Tuple
from dataclasses import dataclass
from datetime import date, datetime
import re
import math
from loguru import logger
from pavlov.pipeline.station_mapper import get_city_for_market, get_tz_for_city, STATION_MAP

_KALSHI_TICKER_DATE_RE = re.compile(r'-(\d{2})([A-Z]{3})(\d{2})(?:-|$)')
_ISO_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')
_MONTH_ABBR = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}


def derive_market_date(market: dict, city: str | None = None) -> Optional[date]:
    """Best-effort settlement date when the row lacks an explicit market_date.

    Order: Kalshi ticker date code (26JUL06) → ISO date in slug/title →
    close_time converted to the station-local calendar date.
    """
    ticker = str(market.get("ticker") or "")
    m = _KALSHI_TICKER_DATE_RE.search(ticker)
    if m:
        mo = _MONTH_ABBR.get(m.group(2).upper())
        if mo:
            try:
                return date(2000 + int(m.group(1)), mo, int(m.group(3)))
            except ValueError:
                pass

    for text in (ticker, str(market.get("poly_market_slug") or ""), str(market.get("title") or "")):
        m = _ISO_DATE_RE.search(text)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue

    close_str = market.get("close_time") or ""
    if close_str:
        try:
            close_dt = datetime.fromisoformat(str(close_str).replace("Z", "+00:00"))
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(get_tz_for_city(city)) if city else None
            local_dt = close_dt.astimezone(tz) if tz else close_dt
            # Daily temp markets close just after the measurement day ends;
            # a close before ~10am local belongs to the previous day.
            if local_dt.hour < 10:
                from datetime import timedelta
                return (local_dt - timedelta(days=1)).date()
            return local_dt.date()
        except (ValueError, TypeError):
            pass
    return None

@dataclass
class NormalizedWeatherEvent:
    platform: str
    market_id: str
    condition_id: Optional[str]
    city: str
    settlement_station: str
    settlement_source: str
    date: date
    local_timezone: str
    observation_window: Tuple[datetime, datetime] | None
    bucket_low_f: float
    bucket_high_f: float
    bucket_label: str
    contract_side: Literal["YES", "NO"]
    contract_url: Optional[str]
    metric: Literal["high", "low"] = "high"


def detect_metric(market: dict) -> str:
    """Determine whether a market settles on the daily HIGH or LOW temperature.

    Kalshi rows carry a ``metric_hint`` derived from the series ticker
    (KXHIGHT*/KXLOWT*). Polymarket rows are classified from the title text.
    """
    hint = (market.get("metric_hint") or "").lower()
    if hint in ("high", "low"):
        return hint
    title = (market.get("title") or "").lower()
    if any(w in title for w in ("low temp", "lowest temp", "minimum temp", "min temp", "daily low", " low of")):
        return "low"
    if re.search(r"\blow\b", title) and "high" not in title:
        return "low"
    return "high"

def parse_bucket_bounds(market: dict) -> Tuple[float, float, str]:
    """Parse the floor and ceiling of the weather bucket."""
    strike_type = market.get("strike_type", "")
    title = market.get("title", "")
    title_lower = title.lower()
    
    if strike_type == "greater":
        threshold = market.get("floor_strike")
        if threshold is None:
            m = re.search(r'>\s*(\d+)', title) or re.search(r'above\s*(\d+)', title_lower)
            threshold = float(m.group(1)) if m else None
        if threshold is None:
            m = re.search(r'(\d+)\s*or\s*above', title_lower)
            threshold = float(m.group(1)) if m else None
        if threshold is None:
            raise ValueError(f"Could not parse 'greater' threshold from {title}")
        # "73 or above" means integer 73, 74... CDF is [72.5, inf)
        # ">73" might mean strictly greater than 73 (74, 75...), but Polymarket usually means >=. We assume inclusive "or above"
        return float(threshold) - 0.5, float("inf"), f">{threshold}"

    if strike_type == "less":
        cap = market.get("ceiling_strike")
        if cap is not None:
            # "70 or below" means integer 70, 69... CDF is (-inf, 70.5)
            return float("-inf"), float(cap) + 0.5, f"<{cap}"
            
        m = re.search(r'<\s*(\d+)', title) or re.search(r'below\s*(\d+)', title_lower)
        if m:
            cap = float(m.group(1))
            return float("-inf"), cap + 0.5, f"<{cap}"
            
        m = re.search(r'(\d+)\s*or\s*below', title_lower)
        if m:
            cap = float(m.group(1))
            return float("-inf"), cap + 0.5, f"<{cap}"
            
        raise ValueError(f"Could not parse 'less' threshold from {title}")

    if strike_type == "between":
        lo_raw = market.get("threshold_lo")
        hi_raw = market.get("threshold_hi")
        if lo_raw is not None and hi_raw is not None:
            # "between 72 and 73" -> CDF [71.5, 73.5]
            return float(lo_raw) - 0.5, float(hi_raw) + 0.5, f"{lo_raw}-{hi_raw}"
            
        m = re.search(r'between\s*(\d+)\s*and\s*(\d+)', title_lower)
        if m:
            return float(m.group(1)) - 0.5, float(m.group(2)) + 0.5, f"{m.group(1)}-{m.group(2)}"
            
        m = re.search(r'(\d+)\s*(?:-|–|—|to)\s*(\d+)', title_lower)
        if m:
            return float(m.group(1)) - 0.5, float(m.group(2)) + 0.5, f"{m.group(1)}-{m.group(2)}"
        
        m = re.search(r'(\d+)-(\d+)', title)
        if m:
            return float(m.group(1)) - 0.5, float(m.group(2)) + 0.5, f"{m.group(1)}-{m.group(2)}"
            
        # Single degree bucket like "71"
        m = re.search(r'^(\d+)$', title)
        if m:
            # "71" -> CDF [70.5, 71.5]
            return float(m.group(1)) - 0.5, float(m.group(1)) + 0.5, str(m.group(1))
            
        raise ValueError(f"Could not parse 'between' thresholds from {title}")
        
    # Fallbacks for simulate mode
    m_between = re.search(r'(\d+)\s*(?:-|–|—|to)\s*(\d+)', title_lower)
    if m_between:
        return float(m_between.group(1)), float(m_between.group(2)), f"{m_between.group(1)}-{m_between.group(2)}"
    m_greater = re.search(r'>(\d+)', title)
    if m_greater:
        return float(m_greater.group(1)), float("inf"), f">{m_greater.group(1)}"
    m_less = re.search(r'<(\d+)', title)
    if m_less:
        return float("-inf"), float(m_less.group(1)), f"<{m_less.group(1)}"
        
    raise ValueError(f"Could not parse bounds from title: {title}")

def normalize_market(market: dict, platform: str) -> Optional[NormalizedWeatherEvent]:
    """Normalize a raw platform market into a standardized event."""
    try:
        title = market.get("title", "")
        
        # 1. Map to Canonical City
        city = market.get("city_hint") or get_city_for_market(title)
        if not city:
            logger.debug(f"Normalization failed: No city match for '{title}'")
            return None
            
        # 2. Get Station and Source
        station_meta = STATION_MAP.get(city)
        if not station_meta:
            logger.debug(f"Normalization failed: No station meta for city '{city}'")
            return None
            
        settlement_station = station_meta.get("station")
        if not settlement_station:
            logger.debug(f"Normalization failed: Missing station code for '{city}'")
            return None
            
        # Standardize source - all Kalshi/Polymarket standard weather bets resolve to NWS CLI (Daily Climate Report)
        settlement_source = "NWS CLI"
        
        # 3. Parse Bucket Bounds
        try:
            lo_f, hi_f, bucket_label = parse_bucket_bounds(market)
        except ValueError as e:
            logger.debug(f"Normalization failed: {e}")
            return None

        # 4. Parse Date
        market_date_str = market.get("market_date") or market.get("date")
        market_date = None
        if market_date_str:
            try:
                if isinstance(market_date_str, date):
                    market_date = market_date_str
                else:
                    market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
            except ValueError:
                logger.debug(f"Invalid date format '{market_date_str}', deriving from ticker/close_time")

        if market_date is None:
            market_date = derive_market_date(market, city)
        if market_date is None:
            logger.debug(f"Normalization failed: Missing date for '{title}'")
            return None

        return NormalizedWeatherEvent(
            platform=platform,
            market_id=market.get("ticker") or market.get("id", "unknown"),
            condition_id=market.get("condition_id"),
            city=city,
            settlement_station=settlement_station,
            settlement_source=settlement_source,
            date=market_date,
            local_timezone="America/New_York", # Default, could be mapped in STATION_MAP
            observation_window=None,
            bucket_low_f=lo_f,
            bucket_high_f=hi_f,
            bucket_label=bucket_label,
            contract_side="YES",
            contract_url=market.get("url"),
            metric=detect_metric(market)
        )
    except Exception as e:
        logger.error(f"Error normalizing market {market.get('title')}: {e}")
        return None
