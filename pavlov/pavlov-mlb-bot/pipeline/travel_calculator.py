"""Travel distance and time-zone fatigue vs previous game location."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from pipeline import mlb_client, park_factors

logger = logging.getLogger(__name__)

# Convention: ET baseline = 0, each step west is −1 (see spec).
TIMEZONES = {"ET": 0, "CT": -1, "MT": -2, "PT": -3}


def _iana_to_bucket(tz_name: str | None) -> int:
    if not tz_name:
        return 0
    z = str(tz_name)
    if z in (
        "America/New_York",
        "America/Detroit",
        "America/Toronto",
        "America/Indiana/Indianapolis",
    ):
        return TIMEZONES["ET"]
    if z in ("America/Chicago",):
        return TIMEZONES["CT"]
    if z in ("America/Denver", "America/Boise", "America/Phoenix"):
        return TIMEZONES["MT"]
    if z in ("America/Los_Angeles", "America/Vancouver"):
        return TIMEZONES["PT"]
    return TIMEZONES["ET"]


def _venue_coords(venue_name: str | None) -> tuple[float | None, float | None, str | None]:
    p = park_factors.get_park(venue_name)
    lat, lon = p.get("lat"), p.get("lon")
    if lat is not None and lon is not None:
        return float(lat), float(lon), p.get("timezone")
    return None, None, p.get("timezone")


def _team_game_on_date(team_id: int, d: date) -> dict | None:
    try:
        sched = mlb_client.get_json(
            "/schedule",
            params={
                "sportId": 1,
                "teamId": int(team_id),
                "startDate": d.isoformat(),
                "endDate": d.isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("travel_calculator: schedule for %s on %s — %s", team_id, d, exc)
        return None
    for bucket in sched.get("dates") or []:
        for g in bucket.get("games") or []:
            teams = g.get("teams") or {}
            hid = (teams.get("home") or {}).get("team", {}).get("id")
            aid = (teams.get("away") or {}).get("team", {}).get("id")
            if hid == team_id or aid == team_id:
                return g
    return None


def _previous_team_game(team_id: int, before: date) -> dict | None:
    start = before - timedelta(days=30)
    end = before - timedelta(days=1)
    try:
        sched = mlb_client.get_json(
            "/schedule",
            params={
                "sportId": 1,
                "teamId": int(team_id),
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("travel_calculator: previous schedule — %s", exc)
        return None
    candidates: list[tuple[str, dict]] = []
    for bucket in sched.get("dates") or []:
        od = bucket.get("date") or ""
        for g in bucket.get("games") or []:
            teams = g.get("teams") or {}
            hid = (teams.get("home") or {}).get("team", {}).get("id")
            aid = (teams.get("away") or {}).get("team", {}).get("id")
            if hid != team_id and aid != team_id:
                continue
            candidates.append((str(g.get("officialDate") or od or ""), g))
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1] if candidates else None


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
    return r * c


def _fatigue_label(penalty: float) -> str:
    if penalty < 0.02:
        return "minimal"
    if penalty < 0.04:
        return "moderate"
    if penalty < 0.06:
        return "elevated"
    return "high"


def calculate_fatigue(
    team_id: int,
    game_date: str | date,
    is_home_game: bool,
    series_game: int | None = None,
) -> dict[str, Any]:
    """
    Road-trip fatigue from distance + time-zone shift since the previous game.
    ``series_game``: game N of series (from schedule); defaults to lookup.
    """
    tid = int(team_id)
    if isinstance(game_date, date):
        gd = game_date
    else:
        gd = datetime.strptime(str(game_date)[:10], "%Y-%m-%d").date()

    zero = {
        "miles": 0.0,
        "tz_shift": 0,
        "direction": "n/a",
        "series_game": series_game or 1,
        "fatigue_penalty": 0.0,
        "label": "minimal",
    }

    if is_home_game:
        return zero

    cur = _team_game_on_date(tid, gd)
    if not cur:
        logger.warning("travel_calculator: no game for team %s on %s", tid, gd)
        return zero

    if series_game is None:
        series_game = int(cur.get("seriesGameNumber") or 1)

    teams = cur.get("teams") or {}
    is_away = (teams.get("away") or {}).get("team", {}).get("id") == tid
    v_cur = (cur.get("venue") or {}).get("name")
    lat2, lon2, tz2 = _venue_coords(v_cur)

    prev = _previous_team_game(tid, gd)
    if not prev:
        return {
            "miles": 0.0,
            "tz_shift": 0,
            "direction": "unknown",
            "series_game": series_game,
            "fatigue_penalty": 0.0,
            "label": "minimal",
            "note": "no_previous_game",
        }

    v_prev = (prev.get("venue") or {}).get("name")
    lat1, lon1, tz1 = _venue_coords(v_prev)

    if None in (lat1, lon1, lat2, lon2):
        miles = 0.0
    else:
        miles = round(haversine_miles(lat1, lon1, lat2, lon2), 1)

    b1 = _iana_to_bucket(tz1)
    b2 = _iana_to_bucket(tz2)
    tz_shift = b2 - b1

    if tz_shift >= 2:
        direction = "east"
    elif tz_shift <= -2:
        direction = "west"
    elif tz_shift == 1:
        direction = "east_light"
    elif tz_shift == -1:
        direction = "west_light"
    else:
        direction = "neutral"

    fatigue_penalty = 0.0
    if miles > 1000:
        fatigue_penalty += 0.018
    if miles > 2000:
        fatigue_penalty += 0.014
    if miles > 2500:
        fatigue_penalty += 0.008

    if tz_shift >= 2:
        fatigue_penalty += 0.022
    elif tz_shift >= 1:
        fatigue_penalty += 0.010
    if tz_shift <= -2:
        fatigue_penalty += 0.007

    if series_game >= 3:
        fatigue_penalty *= 0.6

    fatigue_penalty = round(min(fatigue_penalty, 0.25), 4)

    return {
        "miles": miles,
        "tz_shift": tz_shift,
        "direction": direction,
        "series_game": series_game,
        "fatigue_penalty": fatigue_penalty,
        "label": _fatigue_label(fatigue_penalty),
        "from_venue": v_prev,
        "to_venue": v_cur,
    }


def travel_context(team_abbr: str, as_of_date: str) -> dict:
    """Deprecated shim; use ``calculate_fatigue`` with team id."""
    return {"team": team_abbr, "as_of": as_of_date, "note": "use calculate_fatigue(team_id,...)"}
