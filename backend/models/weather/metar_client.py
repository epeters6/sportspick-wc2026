"""
pipeline/metar_client.py – Real-time surface temperature observations.

Uses the Aviation Weather Center's free METAR API (no key needed) to fetch
current observed temperatures at airport weather stations.

For same-day markets, the observed temperature provides hard constraints:
  - If current temp > threshold and direction == 'above': P(YES) ≈ 1.0
  - If current temp > threshold and direction == 'below': P(YES) ≈ 0.0

Public API
----------
get_current_temp(station_id) -> float | None   (°F)
get_constrained_prob(station_id, threshold_f, direction, current_f) -> float | None
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import requests

import data_paths as dp

logger = logging.getLogger(__name__)

_BASE     = "https://aviationweather.gov/api/data/metar"
_CACHE_TTL = 15 * 60   # 15 minutes — METARs update every 20-60 min

_CACHE_FILE = os.path.join(dp.data_dir(), "metar_cache.json")


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


def _cache_fresh(entry: dict) -> bool:
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched).total_seconds() < _CACHE_TTL
    except (KeyError, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_current_temp(station_id: str) -> float | None:
    """Return the most recent observed temperature at *station_id* in °F.

    Args:
        station_id: ICAO station code, e.g. 'KJFK', 'KORD', 'KMIA'.

    Returns:
        Temperature in °F, or None if unavailable.
    """
    cache = _load_cache()
    key   = station_id.upper()
    if key in cache and _cache_fresh(cache[key]):
        return cache[key]["temp_f"]

    url = f"{_BASE}?ids={station_id}&format=json&taf=false&hours=2"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("METARClient: fetch failed for %s – %s", station_id, exc)
        return None

    if not data or not isinstance(data, list):
        return None

    # data is a list of METAR obs sorted newest-first.
    obs = data[0]
    temp_c = obs.get("temp")   # Celsius
    if temp_c is None:
        return None

    temp_f = round(temp_c * 9 / 5 + 32, 1)

    cache[key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "temp_f":     temp_f,
        "station":    station_id,
        "raw_time":   obs.get("reportTime", ""),
    }
    _save_cache(cache)
    logger.info("METARClient: %s current temp = %.1f°F", station_id, temp_f)
    return temp_f


def get_constrained_prob(
    station_id: str,
    threshold_f: float,
    direction: str,
    hours_left: float,
    metric: str = "high",
) -> float | None:
    """Return a hard probability constraint based on current observed temperature.

    The logic differs fundamentally between HIGH and LOW markets:

    HIGH markets — the peak occurs during the afternoon.
      direction='above': temp_now > threshold → high already exceeded → 0.98
      direction='above': gap > 8°F → extremely unlikely to gain that much → 0.03
      direction='below': temp_now >= threshold → high already above → 0.02

    LOW markets — the trough occurs in the early morning (4-7 AM local).
      Current temperature in the afternoon/evening tells us almost nothing
      about what the overnight low will be.  The only hard constraint is:
      direction='above': temp_now < threshold → low already dropped below → 0.02
      direction='below': temp_now < threshold → low already below threshold → 0.98
      All other LOW cases return None (genuinely uncertain).

    Only applies to same-day markets (hours_left < 18).
    Returns None if observation unavailable or constraint is ambiguous.
    """
    if hours_left >= 18:
        return None

    temp_now = get_current_temp(station_id)
    if temp_now is None:
        return None

    if metric == "low":
        # Low markets: only the "already dropped below" case is hard.
        if direction == "above":
            if temp_now < threshold_f:
                # Current temp is already below threshold — the daily minimum
                # has certainly dipped below it, so P(min > threshold) ≈ 0.02.
                logger.info(
                    "METARClient: %s obs %.1f°F < threshold %.1f°F → P(low above)≈0.02",
                    station_id, temp_now, threshold_f,
                )
                return 0.02
            # temp_now > threshold gives no information about the morning low.
            return None
        elif direction == "below":
            if temp_now < threshold_f:
                # Already observed below threshold — minimum is definitely below.
                logger.info(
                    "METARClient: %s obs %.1f°F < threshold %.1f°F → P(low below)≈0.98",
                    station_id, temp_now, threshold_f,
                )
                return 0.98
            return None
        return None

    # ── HIGH market logic (original, unchanged) ──────────────────────────
    if direction == "above":
        if temp_now > threshold_f:
            logger.info(
                "METARClient: %s obs %.1f°F > threshold %.1f°F → P(above)≈0.98",
                station_id, temp_now, threshold_f,
            )
            return 0.98
        else:
            gap = threshold_f - temp_now
            if gap > 8.0:
                return 0.03
            return None

    elif direction == "below":
        if temp_now >= threshold_f:
            logger.info(
                "METARClient: %s obs %.1f°F >= threshold %.1f°F → P(below)≈0.02",
                station_id, temp_now, threshold_f,
            )
            return 0.02
        return None

    return None
