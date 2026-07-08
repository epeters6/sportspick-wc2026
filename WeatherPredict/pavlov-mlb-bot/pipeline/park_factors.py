"""Park factors + run environment (park JSON + NWS weather modifiers)."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

import data_paths as dp
from pipeline.nws_client import NWSClient

logger = logging.getLogger(__name__)

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARK_FILE = os.path.join(_APP, "data", "park_factors.json")
_TZ_FILE = os.path.join(_APP, "data", "team_timezones.json")

_parks_by_abbr: dict[str, dict] | None = None
_team_meta_by_abbr: dict[str, dict] | None = None
_venue_to_abbr: dict[str, str] | None = None


def _load_team_meta() -> None:
    global _team_meta_by_abbr, _venue_to_abbr
    try:
        with open(_TZ_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        _team_meta_by_abbr, _venue_to_abbr = {}, {}
        return
    _team_meta_by_abbr = {str(k).upper(): v for k, v in raw.items() if isinstance(v, dict)}
    _venue_to_abbr = {}
    for abbr, row in _team_meta_by_abbr.items():
        hp = (row.get("home_park") or "").strip().lower()
        if hp:
            _venue_to_abbr[hp] = abbr


def _load_park_data() -> None:
    global _parks_by_abbr
    _load_team_meta()
    try:
        with open(_PARK_FILE, "r", encoding="utf-8") as fh:
            _parks_by_abbr = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        _parks_by_abbr = {}


_load_park_data()


def load_parks() -> dict:
    """All park rows keyed by team abbreviation."""
    if _parks_by_abbr is None:
        _load_park_data()
    return dict(_parks_by_abbr or {})


def park_for_team(team_abbr: str) -> dict | None:
    data = load_parks()
    row = data.get(team_abbr.upper())
    if not row:
        return None
    return _merge_park_row(team_abbr.upper(), row)


_NEUTRAL_PARK: dict[str, Any] = {
    "run_factor": 1.0,
    "hr_factor": 1.0,
    "altitude_ft": 0,
    "city": None,
    "timezone": "America/New_York",
    "capacity": None,
    "dome": False,
    "_team_abbr": None,
    "venue_match_score": 0.0,
}


def _merge_park_row(abbr: str, row: dict) -> dict:
    out = dict(row)
    out["_team_abbr"] = abbr
    meta = (_team_meta_by_abbr or {}).get(abbr)
    if meta:
        out.setdefault("timezone", meta.get("timezone"))
        out.setdefault("lat", meta.get("lat"))
        out.setdefault("lon", meta.get("lon"))
        out.setdefault("home_park", meta.get("home_park"))
    return out


def get_park(venue_name: str | None) -> dict:
    """
    Fuzzy-match ``venue_name`` to ``team_timezones.json`` ``home_park``,
    merge with ``park_factors.json``. Returns neutral defaults if no decent match.
    """
    if not venue_name or not str(venue_name).strip():
        return dict(_NEUTRAL_PARK)

    target = str(venue_name).strip().lower()
    if _venue_to_abbr and target in _venue_to_abbr:
        abbr = _venue_to_abbr[target]
        row = load_parks().get(abbr)
        if row:
            z = _merge_park_row(abbr, row)
            z["venue_match_score"] = 1.0
            return z

    best_abbr: str | None = None
    best_score = 0.55
    for vkey, abbr in (_venue_to_abbr or {}).items():
        r = SequenceMatcher(None, target, vkey).ratio()
        if r > best_score:
            best_score = r
            best_abbr = abbr

    if best_abbr and (row := load_parks().get(best_abbr)):
        z = _merge_park_row(best_abbr, row)
        z["venue_match_score"] = round(best_score, 3)
        return z

    out = dict(_NEUTRAL_PARK)
    out["venue_query"] = venue_name
    return out


def _iana_to_bucket(tz_name: str | None) -> int:
    """Map IANA zone to user's ET=0 … PT=-3 ladder (integer hour buckets)."""
    if not tz_name:
        return 0
    z = str(tz_name)
    if z in (
        "America/New_York",
        "America/Detroit",
        "America/Toronto",
        "America/Indiana/Indianapolis",
        "America/Puerto_Rico",
    ):
        return 0
    if z in ("America/Chicago",):
        return -1
    if z in ("America/Denver", "America/Boise"):
        return -2
    if z in ("America/Phoenix",):
        return -2
    if z in ("America/Los_Angeles", "America/Vancouver"):
        return -3
    return 0


def _game_day_periods(
    periods: list[dict],
    game_date: date,
    stadium_tz: ZoneInfo,
) -> list[dict]:
    """Evening window (approx. first pitch) in stadium local time."""
    out: list[dict] = []
    gd = game_date.isoformat()
    for p in periods:
        t = p.get("time") or ""
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            continue
        local = dt.astimezone(stadium_tz)
        if local.date() != game_date:
            continue
        hr = local.hour
        if 17 <= hr <= 23:
            out.append(p)
    return out if out else [
        p
        for p in periods
        if str(p.get("time", "")).startswith(gd)
        or str(p.get("time", ""))[:10]
        == gd
    ]


def _wind_components(mph: int | None, direction: str | None) -> tuple[float, float]:
    """Rough ``wind_out`` / ``wind_in`` magnitudes (mph) from NWS vector (wind FROM)."""
    if mph is None or mph <= 0:
        return 0.0, 0.0
    d = (direction or "").strip().upper()
    out_from = {"W", "SW", "NW", "S", "SSW", "NNW", "WSW"}
    in_from = {"E", "NE", "SE", "N", "SSE", "NNE", "ENE"}
    if d in out_from:
        return float(mph), 0.0
    if d in in_from:
        return 0.0, float(mph)
    return 0.0, 0.0


def get_run_environment(
    venue_name: str | None,
    game_date: str | date,
    is_dome: bool,
) -> dict[str, Any]:
    """
    Park run factor plus NWS-driven wind/temp multipliers (dome → weather neutral).
    ``game_date``: ``YYYY-MM-DD`` or ``date``.
    """
    park = get_park(venue_name)
    run_base = float(park.get("run_factor") or 1.0)

    if isinstance(game_date, date):
        gd = game_date
    else:
        gd = datetime.strptime(str(game_date)[:10], "%Y-%m-%d").date()

    dome = bool(is_dome or park.get("dome"))
    if dome:
        return {
            "run_factor": run_base,
            "weather_factor": 1.0,
            "total_factor": run_base,
            "wind_factor": 1.0,
            "temp_factor": 1.0,
            "temp_f": None,
            "wind_speed": None,
            "wind_dir": None,
            "conditions": "dome",
        }

    lat = park.get("lat")
    lon = park.get("lon")
    tz_name = park.get("timezone") or "America/New_York"

    if lat is None or lon is None:
        logger.warning(
            "park_factors: missing lat/lon for venue %r — weather neutral.",
            venue_name,
        )
        return {
            "run_factor": run_base,
            "weather_factor": 1.0,
            "total_factor": run_base,
            "wind_factor": 1.0,
            "temp_factor": 1.0,
            "temp_f": None,
            "wind_speed": None,
            "wind_dir": None,
            "conditions": "no_coordinates",
        }

    try:
        tz = ZoneInfo(str(tz_name))
    except Exception:
        tz = ZoneInfo("America/New_York")

    try:
        periods = NWSClient.get_hourly_forecast(float(lat), float(lon))
    except Exception as exc:
        logger.warning("park_factors: NWS forecast failed — %s", exc)
        return {
            "run_factor": run_base,
            "weather_factor": 1.0,
            "total_factor": run_base,
            "wind_factor": 1.0,
            "temp_factor": 1.0,
            "temp_f": None,
            "wind_speed": None,
            "wind_dir": None,
            "conditions": "nws_error",
        }

    day_p = _game_day_periods(periods, gd, tz)
    if not day_p:
        return {
            "run_factor": run_base,
            "weather_factor": 1.0,
            "total_factor": run_base,
            "wind_factor": 1.0,
            "temp_factor": 1.0,
            "temp_f": None,
            "wind_speed": None,
            "wind_dir": None,
            "conditions": "no_forecast_window",
        }

    temps = [float(p["temp_f"]) for p in day_p if p.get("temp_f") is not None]
    temp_f = round(sum(temps) / len(temps), 1) if temps else None

    w_out_max = 0.0
    w_in_max = 0.0
    wind_dirs: list[str] = []
    labels: list[str] = []
    for p in day_p:
        wo, wi = _wind_components(
            p.get("wind_speed"),
            p.get("wind_dir"),
        )
        w_out_max = max(w_out_max, wo)
        w_in_max = max(w_in_max, wi)
        if p.get("wind_dir"):
            wind_dirs.append(str(p["wind_dir"]))
        if p.get("short_forecast"):
            labels.append(str(p["short_forecast"]))
    wind_spd_rep = int(round(max(w_out_max, w_in_max))) if (w_out_max or w_in_max) else None
    wind_dir_rep = wind_dirs[0] if wind_dirs else None
    conditions = "; ".join(sorted(set(labels))[:3]) if labels else None

    wind_factor = 1.0
    if w_out_max > 15:
        wind_factor = 1.10
    elif w_out_max > 10:
        wind_factor = 1.06
    elif w_in_max > 10:
        wind_factor = 0.94

    temp_factor = 1.0
    if temp_f is not None:
        if temp_f < 45:
            temp_factor = 0.93
        elif temp_f < 55:
            temp_factor = 0.97
        elif temp_f > 85:
            temp_factor = 1.03

    weather_factor = wind_factor * temp_factor
    total = run_base * weather_factor

    return {
        "run_factor": run_base,
        "wind_factor": wind_factor,
        "temp_factor": temp_factor,
        "weather_factor": round(weather_factor, 4),
        "total_factor": round(total, 4),
        "temp_f": temp_f,
        "wind_speed": wind_spd_rep,
        "wind_dir": wind_dir_rep,
        "conditions": conditions,
    }
