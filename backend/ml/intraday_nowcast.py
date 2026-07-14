import logging
from datetime import datetime, timezone
import os
import sys

# Ensure pavlov is in path to import metar_client
pavlov_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "pavlov")
if pavlov_path not in sys.path:
    sys.path.insert(0, pavlov_path)

from pipeline.metar_client import get_current_temp

logger = logging.getLogger(__name__)

_METAR_HISTORY_URL = "https://aviationweather.gov/api/data/metar"


def get_current_obs(city: str) -> dict:
    """Return today's observed temperature extremes so far for a city's station.

    Uses the station-local calendar date so evening UTC hours don't leak into
    "tomorrow". Returns sentinel values when data is unavailable so callers can
    check ``high_so_far > -999`` / ``low_so_far < 999``.
    """
    result = {"high_so_far": -999.0, "low_so_far": 999.0}
    try:
        from zoneinfo import ZoneInfo
        import requests
        from pipeline.station_mapper import STATION_MAP, get_tz_for_city

        station_meta = STATION_MAP.get(city)
        if not station_meta:
            return result
        station_id = station_meta["station"]
        local_today = datetime.now(ZoneInfo(get_tz_for_city(city))).date()

        url = f"{_METAR_HISTORY_URL}?ids={station_id}&format=json&taf=false&hours=30"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return result

        tz = ZoneInfo(get_tz_for_city(city))
        temps: list[float] = []
        for obs in data:
            temp_c = obs.get("temp")
            report_time = obs.get("reportTime", "")
            if temp_c is None or not report_time:
                continue
            try:
                obs_dt = datetime.fromisoformat(report_time.replace("Z", "+00:00"))
                if obs_dt.tzinfo is None:
                    obs_dt = obs_dt.replace(tzinfo=timezone.utc)
                if obs_dt.astimezone(tz).date() == local_today:
                    temps.append(float(temp_c) * 9 / 5 + 32)
            except (ValueError, TypeError):
                continue

        if temps:
            result["high_so_far"] = round(max(temps), 1)
            result["low_so_far"] = round(min(temps), 1)
    except Exception as exc:
        logger.warning(f"get_current_obs failed for {city}: {exc}")
    return result

def apply_hrrr_nowcast_shift(city: str, station_id: str, base_mean: float, base_spread: float) -> tuple[float, float]:
    """
    Intraday nowcast helper for same-day daily-high markets.

    Historically this compared live METAR to ``base_mean`` (the daily-high
    forecast) and applied that full deviation after the diurnal peak hour —
    which collapses a ~95°F high forecast to the evening observation (~76°F).
    That is wrong: ``forecast_temp_at_this_hour`` must be an hourly forecast,
    not the daily max.

    Until we have a real hourly forecast path, only tighten spread as the
    local day progresses near/after the usual peak, and leave the mean alone.
    Impossible low buckets are already zeroed via ``mask_impossible_buckets``
    using ``high_so_far``.
    """
    try:
        from zoneinfo import ZoneInfo
        from pipeline.station_mapper import get_tz_for_city
        from backend.ml.diurnal_curve import elapsed_warming_fraction

        local_hour = datetime.now(ZoneInfo(get_tz_for_city(city))).hour
        confidence = elapsed_warming_fraction(float(local_hour))
        max_spread_tightening = 0.6
        min_spread_floor = 1.5
        new_spread = max(base_spread * (1 - max_spread_tightening * confidence), min_spread_floor)
        if abs(new_spread - base_spread) > 1e-6:
            logger.info(
                "NOWCAST SPREAD ONLY [%s - %s]: local_hour=%s confidence=%.2f "
                "spread %.1f -> %.1f (mean unchanged at %.1f)",
                city, station_id, local_hour, confidence, base_spread, new_spread, base_mean,
            )
        return base_mean, new_spread
    except Exception as exc:
        logger.warning(f"Nowcast spread tighten failed for {station_id}: {exc}")
        return base_mean, base_spread
