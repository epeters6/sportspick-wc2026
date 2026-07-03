"""
pipeline/owm_client.py – OpenWeatherMap forecast wrapper.

Uses the free OWM One Call API 3.0 (or 2.5 fallback) to fetch hourly
forecasts as a second opinion alongside NWS data.

Set OWM_API_KEY in your .env / Railway Variables to enable.
If the key is absent the client silently returns None so the signal engine
can fall back to NWS-only mode.

Public API
----------
get_predicted_high(city, date_str) -> float | None
get_predicted_low(city, date_str)  -> float | None
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

_API_KEY  = os.environ.get("OWM_API_KEY", "").strip()
_BASE_2_5 = "https://api.openweathermap.org/data/2.5/forecast"   # free tier
_CACHE_TTL = 20 * 60   # 20 minutes

_CACHE_FILE = os.path.join(dp.data_dir(), "owm_cache.json")


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
    return f"owm_{city.lower().replace(' ', '_')}"


def _cache_fresh(entry: dict) -> bool:
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        fetched = datetime.fromisoformat(ts)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched).total_seconds() < _CACHE_TTL
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Internal fetch
# ---------------------------------------------------------------------------

def _fetch_hourly(city: str) -> list[dict] | None:
    """Return list of {time_utc, temp_f} dicts for the next 5 days, or None."""
    if not _API_KEY:
        return None

    station = STATION_MAP.get(city)
    if not station:
        return None

    cache = _load_cache()
    key   = _cache_key(city)
    if key in cache and _cache_fresh(cache[key]):
        return cache[key]["periods"]

    lat, lon = station["lat"], station["lon"]
    url = (
        f"{_BASE_2_5}?lat={lat}&lon={lon}"
        f"&units=imperial&appid={_API_KEY}&cnt=40"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("OWMClient: fetch failed for %s – %s", city, exc)
        return None

    periods = [
        {
            "time_utc": item["dt"],           # unix timestamp
            "temp_f":   item["main"]["temp"],  # °F (imperial)
            "feels_f":  item["main"]["feels_like"],
        }
        for item in data.get("list", [])
    ]

    cache[key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "periods":    periods,
    }
    _save_cache(cache)
    logger.info("OWMClient: cached %d periods for %s.", len(periods), city)
    return periods


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_predicted_high(city: str, date_str: str) -> float | None:
    """Return OWM predicted high for *city* on *date_str* (YYYY-MM-DD).

    Only future hours are used for today's date (same logic as NWS client).
    Returns None if OWM is unavailable or the key is not set.
    """
    periods = _fetch_hourly(city)
    if not periods:
        return None

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_ts    = time.time()

    target_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    target_start = target_date.timestamp()
    target_end   = target_start + 86400

    filtered = [
        p for p in periods
        if target_start <= p["time_utc"] < target_end
        and (date_str != today_str or p["time_utc"] >= now_ts)
    ]
    if not filtered:
        return None

    return max(p["temp_f"] for p in filtered)


def get_predicted_low(city: str, date_str: str) -> float | None:
    """Return OWM predicted low for *city* on *date_str* (YYYY-MM-DD).

    Returns None if OWM is unavailable or the key is not set.
    """
    periods = _fetch_hourly(city)
    if not periods:
        return None

    target_date  = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    target_start = target_date.timestamp()
    target_end   = target_start + 86400

    filtered = [
        p for p in periods
        if target_start <= p["time_utc"] < target_end
    ]
    if not filtered:
        return None

    return min(p["temp_f"] for p in filtered)


def available() -> bool:
    """Return True if OWM_API_KEY is configured."""
    return bool(_API_KEY)
