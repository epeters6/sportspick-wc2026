from typing import Literal, Optional, Tuple
from dataclasses import dataclass
from datetime import date, datetime
import re
import math
from loguru import logger
from pavlov.pipeline.station_mapper import get_city_for_market, STATION_MAP

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
        if not market_date_str:
            logger.debug(f"Normalization failed: Missing date for '{title}'")
            return None
            
        try:
            if isinstance(market_date_str, date):
                market_date = market_date_str
            else:
                market_date = datetime.strptime(market_date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.debug(f"Normalization failed: Invalid date format '{market_date_str}'")
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
            contract_url=market.get("url")
        )
    except Exception as e:
        logger.error(f"Error normalizing market {market.get('title')}: {e}")
        return None
