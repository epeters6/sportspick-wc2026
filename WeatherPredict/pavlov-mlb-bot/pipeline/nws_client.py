"""
NOAA National Weather Service client for MLB run environment (lat/lon → hourly grid).

Uses ``/points/{lat},{lon}`` so no city/station table is required.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

import requests

import data_paths as dp

logger = logging.getLogger(__name__)

_BASE = "https://api.weather.gov"
_HEADERS = {"User-Agent": "pavlov-mlb-bot/1.0 (pavlov-mlb-bot@local)"}
_CACHE_TTL = 15 * 60
_CACHE_FILE = os.path.join(dp.data_dir(), "forecast_cache.json")


def _parse_period_time(time_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _parse_wind_speed(raw: str | None) -> int | None:
    if not raw:
        return None
    m = re.search(r"\d+", str(raw))
    return int(m.group()) if m else None


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


def _cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 4)}_{round(lon, 4)}"


def _cache_entry_fresh(entry: dict) -> bool:
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


def _http_get(url: str) -> dict:
    logger.debug("NWS: GET %s", url)
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


class NWSClient:
    """Thin wrapper for hourly forecast fetches (callable as ``NWSClient``)."""

    @staticmethod
    def get_hourly_forecast(lat: float, lon: float) -> list[dict]:
        """Hourly periods for ~7 days: time (ISO), temp_f, wind_speed (mph), wind_dir, short_forecast."""
        cache = _load_cache()
        key = _cache_key(lat, lon)
        if key in cache and _cache_entry_fresh(cache[key]):
            return list(cache[key]["periods"])

        pt = _http_get(f"{_BASE}/points/{lat},{lon}")
        hourly_url = (pt.get("properties") or {}).get("forecastHourly")
        if not hourly_url:
            logger.warning("NWS: no forecastHourly URL for %s,%s", lat, lon)
            return []

        data = _http_get(hourly_url)
        raw_periods: list[dict] = (data.get("properties") or {}).get("periods", [])
        periods: list[dict] = []
        for p in raw_periods:
            periods.append(
                {
                    "time": p.get("startTime", ""),
                    "temp_f": p.get("temperature"),
                    "wind_speed": _parse_wind_speed(p.get("windSpeed")),
                    "wind_dir": p.get("windDirection"),
                    "short_forecast": p.get("shortForecast", ""),
                }
            )

        cache[key] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "periods": periods,
        }
        _save_cache(cache)
        logger.info("NWS: cached %d hourly periods for %s,%s.", len(periods), lat, lon)
        return periods
