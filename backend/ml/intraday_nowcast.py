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
    Intraday Nowcasting Engine.
    For same-day contracts, this pulls the live METAR observation and 
    applies a HRRR-style intraday shift to the ensemble forecast.
    
    If the current station temperature is running hotter than expected for this hour, 
    the mean_f is shifted upwards, and the spread is tightened (since we are closer to close).
    """
    try:
        current_temp = get_current_temp(station_id)
    except Exception as exc:
        logger.warning(f"Nowcast failed to fetch METAR for {station_id}: {exc}")
        return base_mean, base_spread

    if current_temp is None:
        return base_mean, base_spread
        
    now = datetime.now(timezone.utc)
    
    # Diurnal Curve adjustment
    from backend.ml.diurnal_curve import nowcast_adjustment
    
    # Normally we would fetch the station's climatology or forecast model's t_min/t_max.
    # For now, we use the symmetric fallback with typical t_min/t_max.
    # In a full production system, we'd pull `climatology` for the station.
    
    # We must convert current time to local standard time for the station. 
    # For simplicity, we'll use UTC hour offset by an approximate -5 (EST) or pass it directly.
    # Since we don't have timezone data here, we'll assume a generic daytime curve.
    current_hour_local = (now.hour - 5) % 24  # rough approximation for EST
    
    shift, new_spread = nowcast_adjustment(
        observed_temp=current_temp,
        forecast_temp_at_this_hour=base_mean,
        current_hour=current_hour_local,
        forecast_spread=base_spread,
    )
    
    new_mean = base_mean + shift
    
    if shift != 0.0:
        logger.info(
            f"🌩️ NOWCAST SHIFT [{city} - {station_id}]: "
            f"Observed {current_temp}°F. Shifting mean {base_mean}°F -> {new_mean}°F, spread {base_spread:.1f} -> {new_spread:.1f}"
        )
        
    return new_mean, new_spread
