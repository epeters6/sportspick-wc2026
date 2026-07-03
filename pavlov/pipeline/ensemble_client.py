"""
pipeline/ensemble_client.py – Multi-model ensemble forecast client.

Fetches real ensemble members from Open-Meteo (free, no API key needed).
Each model is fetched separately so its members can be weighted by historical
forecast skill before the probability vote is computed.

Model weights (relative):
  ecmwf_ifs04   – 2.0   (51 members, gold standard for medium-range)
  gfs025        – 1.0   (31 members, NOAA operational)
  icon_seamless – 1.0   (40 members, DWD; strong over Europe/N. America)
  gem_global    – 0.75  (21 members, CMC; useful but less skill than others)

Public API
----------
get_ensemble_prob(city, date_str, threshold_f, direction, ...) -> dict | None
    Returns {"prob": float, "members": int, "mean_f": float, "spread_f": float}
update_bias(city, metric, error_f)
    EMA-update the city bias table and persist it to disk.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import requests

from pipeline.station_mapper import STATION_MAP

import data_paths as dp

logger = logging.getLogger(__name__)

_BASE      = "https://ensemble-api.open-meteo.com/v1/ensemble"
_CACHE_TTL = 20 * 60   # 20 minutes

_CACHE_FILE = os.path.join(dp.data_dir(), "ensemble_cache.json")
_BIAS_FILE  = os.path.join(dp.logs_dir(), "ensemble_bias.json")

_STORAGE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Model registry — name → relative vote weight
# Fetched one at a time so weights can be applied cleanly.
# ---------------------------------------------------------------------------

_MODELS: dict[str, float] = {
    "ecmwf_ifs04":    2.0,   # 51 members — gold standard
    "gfs025":         1.0,   # 31 members
    "icon_seamless":  1.0,   # 40 members
    "gem_global":     0.75,  # 21 members
}

# Minimum inter-request gap for Open-Meteo (generous free tier, but be polite).
_OM_REQUEST_GAP = 0.3   # seconds


# ---------------------------------------------------------------------------
# Bias table — per-city EMA correction (°F).
# Loaded from disk at module load; persisted on every update.
# Positive = model runs too warm (subtract from members).
# ---------------------------------------------------------------------------

def _load_bias() -> dict[str, float]:
    defaults: dict[str, float] = {
        "New York": 0.0, "Chicago": 0.0, "Miami": 0.0, "Denver": 0.0,
        "Dallas": 0.0, "Phoenix": 0.0, "Seattle": 0.0, "Boston": 0.0,
        "Atlanta": 0.0, "Las Vegas": 0.0, "Minneapolis": 0.0,
        "Los Angeles": 0.0, "San Francisco": 0.0, "Washington DC": 0.0,
        "Philadelphia": 0.0, "Austin": 0.0, "Houston": 0.0,
        "San Antonio": 0.0, "Oklahoma City": 0.0,
    }
    try:
        with open(_BIAS_FILE, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        defaults.update({k: float(v) for k, v in on_disk.items()})
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    return defaults


_BIAS: dict[str, float] = _load_bias()


@contextmanager
def isolated_storage(cache_file: str, bias_file: str):
    """Use alternate ensemble cache + bias files (e.g. Polymarket pipeline).

    Serialized with a lock so Kalshi and Poly never clobber in-memory _BIAS.
    """
    global _CACHE_FILE, _BIAS_FILE, _BIAS
    with _STORAGE_LOCK:
        prev_cache = _CACHE_FILE
        prev_bias_path = _BIAS_FILE
        prev_bias = dict(_BIAS)
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        os.makedirs(os.path.dirname(bias_file), exist_ok=True)
        try:
            _CACHE_FILE = cache_file
            _BIAS_FILE = bias_file
            _BIAS = _load_bias()
            yield
        finally:
            _save_bias()
            _CACHE_FILE = prev_cache
            _BIAS_FILE = prev_bias_path
            _BIAS = prev_bias


def _save_bias() -> None:
    os.makedirs(os.path.dirname(_BIAS_FILE), exist_ok=True)
    with open(_BIAS_FILE, "w", encoding="utf-8") as fh:
        json.dump(_BIAS, fh, indent=2)


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


def _cache_key(city: str, metric: str) -> str:
    # v2 prefix keeps new per-model format separate from old flat-list entries.
    return f"ens_v2_{city.lower().replace(' ', '_')}_{metric}"


def _cache_fresh(entry: dict) -> bool:
    try:
        fetched = datetime.fromisoformat(entry.get("fetched_at", ""))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched).total_seconds() < _CACHE_TTL
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Per-model member fetching
# ---------------------------------------------------------------------------

_last_om_request: float = 0.0


def _om_rate_limit() -> None:
    global _last_om_request
    elapsed = time.monotonic() - _last_om_request
    if elapsed < _OM_REQUEST_GAP:
        time.sleep(_OM_REQUEST_GAP - elapsed)
    _last_om_request = time.monotonic()


def _fetch_model(
    lat: float,
    lon: float,
    daily_var: str,
    model: str,
    bias: float,
) -> dict[str, list[float]] | None:
    """Fetch ensemble members for a single model.

    Returns {date_str: [member_values_in_°F]} or None on failure.
    """
    url = (
        f"{_BASE}?latitude={lat}&longitude={lon}"
        f"&daily={daily_var}"
        f"&temperature_unit=fahrenheit"
        f"&forecast_days=7"
        f"&models={model}"
    )
    data: dict | None = None
    for attempt in range(3):
        _om_rate_limit()
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 429:
                wait = 2.0 * (attempt + 1)
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        wait = float(ra)
                    except (TypeError, ValueError):
                        pass
                logger.warning(
                    "EnsembleClient: %s rate limited (429) — sleeping %.1fs (attempt %d/3)",
                    model,
                    wait,
                    attempt + 1,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as exc:
            logger.warning("EnsembleClient: %s fetch failed – %s", model, exc)
            return None

    if not data:
        logger.warning(
            "EnsembleClient: %s fetch failed — repeated HTTP 429 (members omitted for this model)",
            model,
        )
        return None

    daily     = data.get("daily", {})
    time_list = daily.get("time", [])
    if not time_list:
        return None

    by_date: dict[str, list[float]] = {d: [] for d in time_list}

    for var_key, values in daily.items():
        if not var_key.startswith(daily_var + "_member"):
            continue
        if not isinstance(values, list):
            continue
        for i, v in enumerate(values):
            if i < len(time_list) and v is not None:
                by_date[time_list[i]].append(float(v) - bias)

    n = max((len(vs) for vs in by_date.values()), default=0)
    if n == 0:
        return None

    logger.debug("EnsembleClient: %s → %d members/day", model, n)
    return by_date


# ---------------------------------------------------------------------------
# Combined multi-model fetch (with caching)
# ---------------------------------------------------------------------------

def _fetch_members(
    city: str,
    metric: str,
) -> dict[str, dict[str, list[float]]] | None:
    """Return {date_str: {model_name: [member_values]}} for all models.

    Fetches each model separately so vote weights can be applied.
    Results are cached per city+metric for _CACHE_TTL seconds.
    """
    station = STATION_MAP.get(city)
    if not station:
        return None

    cache = _load_cache()
    key   = _cache_key(city, metric)
    if key in cache and _cache_fresh(cache[key]):
        return cache[key]["by_date"]

    lat, lon   = station["lat"], station["lon"]
    daily_var  = "temperature_2m_max" if metric == "high" else "temperature_2m_min"
    bias       = _BIAS.get(city, 0.0)

    # Fetch each model and merge into {date: {model: [values]}}.
    combined: dict[str, dict[str, list[float]]] = {}
    total_members = 0

    for model in _MODELS:
        by_date = _fetch_model(lat, lon, daily_var, model, bias)
        if not by_date:
            continue
        for date, members in by_date.items():
            if date not in combined:
                combined[date] = {}
            combined[date][model] = members
        total_members += max((len(vs) for vs in by_date.values()), default=0)

    if total_members == 0:
        logger.warning("EnsembleClient: no members fetched for %s %s", city, metric)
        cache[key] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "by_date":    {},
        }
        _save_cache(cache)
        return None

    cache[key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "by_date":    combined,
    }
    _save_cache(cache)

    # Log today's summary.
    today = list(combined.keys())[0] if combined else ""
    if today:
        today_all: list[float] = []
        for model_members in combined.get(today, {}).values():
            today_all.extend(model_members)
        if today_all:
            mean = sum(today_all) / len(today_all)
            logger.info(
                "EnsembleClient: %s %s – %d total members, today mean=%.1f°F.",
                city, metric, len(today_all), mean,
            )

    return combined


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ensemble_prob(
    city: str,
    date_str: str,
    threshold_f: float,
    direction: str,
    metric: str = "high",
    threshold_lo: float | None = None,
    threshold_hi: float | None = None,
) -> dict | None:
    """Calculate weighted-ensemble probability for a Kalshi weather market.

    Each model's votes are multiplied by its weight before summing.
    ECMWF IFS (weight=2.0) therefore contributes twice as much as GFS/ICON.

    Returns:
        {
            "prob":     float  – weighted probability (0–1),
            "members":  int    – total raw member count used,
            "mean_f":   float  – unweighted mean across all members,
            "spread_f": float  – unweighted standard deviation (spread),
        }
        or None if data unavailable.
    """
    by_date_all = _fetch_members(city, metric)
    if not by_date_all:
        return None

    date_models = by_date_all.get(date_str, {})
    if not date_models:
        logger.debug(
            "EnsembleClient: no data for %s %s on %s (available: %s)",
            city, metric, date_str, list(by_date_all.keys())[:4],
        )
        return None

    weighted_votes = 0.0
    weighted_total = 0.0
    all_members:   list[float] = []

    for model, weight in _MODELS.items():
        members = date_models.get(model, [])
        if not members:
            continue

        if direction == "above":
            votes = sum(1 for v in members if v > threshold_f)
        elif direction == "below":
            votes = sum(1 for v in members if v < threshold_f)
        elif direction == "in_range" and threshold_lo is not None and threshold_hi is not None:
            votes = sum(1 for v in members if threshold_lo <= v <= threshold_hi)
        else:
            continue

        weighted_votes += weight * votes
        weighted_total += weight * len(members)
        all_members.extend(members)

    if weighted_total == 0 or len(all_members) < 5:
        return None

    prob = weighted_votes / weighted_total

    n        = len(all_members)
    mean_f   = sum(all_members) / n
    variance = sum((v - mean_f) ** 2 for v in all_members) / n
    spread_f = variance ** 0.5

    logger.debug(
        "EnsembleClient: %s %s %s %.1f°F on %s → %.1f%% (weighted, %d members).",
        city, metric, direction, threshold_f, date_str,
        prob * 100, n,
    )

    return {
        "prob":     round(prob, 4),
        "members":  n,
        "mean_f":   round(mean_f, 1),
        "spread_f": round(spread_f, 1),
    }


def update_bias(city: str, metric: str, error_f: float) -> None:
    """EMA-update the city bias and persist to disk.

    Args:
        city:    City name matching STATION_MAP.
        metric:  'high' or 'low'.
        error_f: (ensemble_mean − actual_temp) in °F. Positive = ran too warm.
    """
    if city in _BIAS:
        old = _BIAS[city]
        _BIAS[city] = round(old * 0.9 + error_f * 0.1, 2)
        logger.info(
            "EnsembleClient: %s bias %.2f → %.2f°F (error=%+.1f°F, metric=%s).",
            city, old, _BIAS[city], error_f, metric,
        )
        _save_bias()
