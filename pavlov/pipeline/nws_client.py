"""
pipeline/nws_client.py – NOAA National Weather Service REST API wrapper.

BASE: https://api.weather.gov

Public API
----------
get_hourly_forecast(city)            -> list[dict]
get_predicted_high(city, date_str)   -> dict
get_predicted_low(city, date_str)    -> dict

All city names must be keys in pipeline.station_mapper.STATION_MAP.

Forecast data is cached to /data/forecast_cache.json for 30 minutes.
No API key is required; NWS only asks for a descriptive User-Agent.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

from pipeline.station_mapper import STATION_MAP

import data_paths as dp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = "https://api.weather.gov"
_HEADERS = {"User-Agent": "pavlov-weather-bot/1.0 (weather-bot@local)"}
_CACHE_TTL = 15 * 60   # 15 minutes — keeps forecasts fresh within each cycle

_CACHE_FILE = os.path.join(dp.data_dir(), "forecast_cache.json")

def _parse_period_time(time_str: str) -> datetime:
    """Parse an NWS period timestamp to a timezone-aware UTC datetime.

    NWS returns timestamps with local offsets, e.g. '2026-05-17T14:00:00-04:00'.
    We must parse them properly rather than comparing as raw strings, otherwise
    '14:00:00-04:00' (2 PM ET = 6 PM UTC) appears to be 'before'
    '17:57:00+00:00' (5:57 PM UTC) when compared lexicographically.
    """
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, default=str)


def _cache_key(city: str) -> str:
    return city.lower().replace(" ", "_")


def _cache_entry_fresh(entry: dict) -> bool:
    """Return True if the cache entry was written within _CACHE_TTL seconds."""
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        fetched = datetime.fromisoformat(ts)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        return age < _CACHE_TTL
    except (ValueError, TypeError):
        return False

# ---------------------------------------------------------------------------
# Internal: station lookup + HTTP
# ---------------------------------------------------------------------------

def _get_station(city: str) -> dict:
    """Return the STATION_MAP entry for *city*, raising KeyError if unknown."""
    try:
        return STATION_MAP[city]
    except KeyError:
        known = ", ".join(sorted(STATION_MAP.keys()))
        raise KeyError(
            f"NWSClient: unknown city {city!r}. Known cities: {known}"
        ) from None


def _http_get(url: str) -> dict:
    """GET *url* with NWS User-Agent, raise on non-2xx."""
    logger.debug("NWSClient: GET %s", url)
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Hourly forecast parsing
# ---------------------------------------------------------------------------

def _parse_wind_speed(raw: str | None) -> int | None:
    """Extract the leading integer from strings like '12 mph' or '5 to 10 mph'."""
    if not raw:
        return None
    import re
    m = re.search(r"\d+", raw)
    return int(m.group()) if m else None


def _parse_period(period: dict) -> dict:
    """Convert a single NWS hourly period into our flat schema."""
    return {
        "time":           period.get("startTime", ""),
        "temp_f":         period.get("temperature"),           # int °F
        "wind_speed":     _parse_wind_speed(period.get("windSpeed")),
        "short_forecast": period.get("shortForecast", ""),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_hourly_forecast(city: str) -> list[dict]:
    """Return hourly forecast periods for *city* for the next ~7 days.

    Each item in the returned list has:
        time            – ISO-8601 start time of the hour
        temp_f          – temperature in °F (int)
        wind_speed      – leading wind speed in mph (int or None)
        short_forecast  – NWS short description, e.g. "Mostly Cloudy"

    Results are cached per city in /data/forecast_cache.json for 30 minutes.
    """
    station = _get_station(city)
    cache = _load_cache()
    key = _cache_key(city)

    if key in cache and _cache_entry_fresh(cache[key]):
        periods: list[dict] = cache[key]["periods"]
        logger.info(
            "NWSClient: returning %d cached hourly periods for %s.", len(periods), city
        )
        return periods

    office  = station["nws_office"]
    grid_x  = station["grid_x"]
    grid_y  = station["grid_y"]
    url = f"{_BASE}/gridpoints/{office}/{grid_x},{grid_y}/forecast/hourly"

    logger.info("NWSClient: fetching hourly forecast for %s …", city)
    data = _http_get(url)
    raw_periods: list[dict] = (
        data.get("properties", {}).get("periods", [])
    )
    periods = [_parse_period(p) for p in raw_periods]

    cache[key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "periods":    periods,
    }
    _save_cache(cache)

    logger.info("NWSClient: cached %d hourly periods for %s.", len(periods), city)
    return periods


def get_predicted_high(city: str, date_str: str) -> dict:
    """Return the predicted high temperature for *city* on *date_str*.

    For today's date, only future hourly periods are considered — past hours
    are already settled and don't affect whether an end-of-day market resolves
    YES or NO.  For future dates all hours are used.

    Args:
        city:     A key from STATION_MAP, e.g. "Chicago".
        date_str: Date in YYYY-MM-DD format, e.g. "2025-05-17".

    Returns:
        {
            "predicted_high_f":   int   – highest forecast temp on that date,
            "recorded_at_hour":   str   – ISO-8601 time of the peak hour,
            "margin_above_avg":   float – degrees above the daily mean temp,
        }

    Raises:
        ValueError if no forecast hours are available for *date_str*.
    """
    periods = get_hourly_forecast(city)
    day_periods = [p for p in periods if p["time"].startswith(date_str)]

    if not day_periods:
        raise ValueError(
            f"NWSClient: no forecast data for {city!r} on {date_str}. "
            "The date may be beyond the 7-day forecast window."
        )

    # For today only, restrict to future hours so we model the remaining
    # trajectory — not hours that have already passed.
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if date_str == today_str:
        now_utc = datetime.now(timezone.utc)
        future = [
            p for p in day_periods
            if _parse_period_time(p["time"]) >= now_utc
        ]
        if future:  # fall back to all hours if nothing future (late evening)
            day_periods = future

    # Filter out periods with missing temps before sorting.
    valid = [p for p in day_periods if p["temp_f"] is not None]
    if not valid:
        raise ValueError(
            f"NWSClient: all temperatures missing for {city!r} on {date_str}."
        )

    peak = max(valid, key=lambda p: p["temp_f"])
    temps = [p["temp_f"] for p in valid]
    avg_temp = sum(temps) / len(temps)
    margin = round(peak["temp_f"] - avg_temp, 2)

    return {
        "predicted_high_f":  peak["temp_f"],
        "recorded_at_hour":  peak["time"],
        "margin_above_avg":  margin,
    }


def get_predicted_low(city: str, date_str: str) -> dict:
    """Return the predicted low temperature for *city* on *date_str*.

    For today's date, only future hourly periods are considered.
    """
    periods = get_hourly_forecast(city)
    day_periods = [p for p in periods if p["time"].startswith(date_str)]

    if not day_periods:
        raise ValueError(
            f"NWSClient: no forecast data for {city!r} on {date_str}. "
            "The date may be beyond the 7-day forecast window."
        )

    # Same future-hours filter as get_predicted_high.
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if date_str == today_str:
        now_utc = datetime.now(timezone.utc)
        future = [
            p for p in day_periods
            if _parse_period_time(p["time"]) >= now_utc
        ]
        if future:
            day_periods = future

    valid = [p for p in day_periods if p["temp_f"] is not None]
    if not valid:
        raise ValueError(
            f"NWSClient: all temperatures missing for {city!r} on {date_str}."
        )

    trough = min(valid, key=lambda p: p["temp_f"])

    return {
        "predicted_low_f":  trough["temp_f"],
        "recorded_at_hour": trough["time"],
    }
