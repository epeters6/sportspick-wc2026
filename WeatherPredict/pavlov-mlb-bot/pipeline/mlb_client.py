"""
MLB Stats API client (statsapi.mlb.com). No API key — identifies with User-Agent.

``BASE_URL`` defaults from ``CONFIG['MLB_API_BASE']`` (see ``config.py``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import requests

import data_paths as dp
from config import CONFIG

logger = logging.getLogger(__name__)

BASE_URL = str(CONFIG.get("MLB_API_BASE") or "https://statsapi.mlb.com/api/v1").rstrip("/")
_USER_AGENT = str(CONFIG.get("MLB_API_USER_AGENT") or "pavlov-mlb-bot/1.0")
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": _USER_AGENT})


def init() -> None:
    """Process startup: ensure persistent dirs exist and log API configuration."""
    import data_paths as dp

    dp.ensure_state_dirs()
    logger.info(
        "mlb_client: init — state_root=%s  MLB_API_BASE=%s",
        dp.state_root(),
        BASE_URL,
    )

_CACHE_TTL_SEC = 30 * 60
_GAME_HYDRATE = "team,lineups,probablePitcher,weather,linescore,venue"


def _games_cache_path() -> str:
    return os.path.join(dp.data_dir(), "games_cache.json")


def _load_games_cache_file() -> dict:
    path = _games_cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_games_cache_file(data: dict) -> None:
    path = _games_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def get_json(path: str, params: dict | list | None = None) -> Any:
    """GET *path* (with leading ``/``) under ``BASE_URL``."""
    url = f"{BASE_URL}{path if path.startswith('/') else '/' + path}"
    r = _SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_game_feed_live(game_pk: int) -> dict[str, Any]:
    """Live feed (v1.1) with ``gameData.status`` and ``liveData.linescore`` runs."""
    url = f"https://statsapi.mlb.com/api/v1.1/game/{int(game_pk)}/feed/live"
    r = _SESSION.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def _current_season_year(d: date | None = None) -> int:
    if d is None:
        d = date.today()
    return d.year


def _parse_ip_to_outs(ip_str: str | None) -> int:
    """Convert innings pitched string (e.g. ``6.0``, ``5.1``) to total outs."""
    if not ip_str:
        return 0
    s = str(ip_str).strip()
    if not s:
        return 0
    if "." in s:
        whole, frac = s.split(".", 1)
    else:
        whole, frac = s, "0"
    try:
        w = int(whole)
        f = int(frac[0]) if frac else 0
    except ValueError:
        return 0
    if f not in (0, 1, 2):
        f = min(2, max(0, f))
    return w * 3 + f


def _outs_to_ip_float(outs: int) -> float:
    return round(outs / 3, 3)


def _float_or_none(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_avg(s: str | None) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in (".---", "-.--"):
        return None
    if s.startswith("."):
        s = "0" + s
    try:
        return float(s)
    except ValueError:
        return None


def _fetch_pitchers_season_era_map(
    pitcher_ids: list[int], season: int
) -> dict[int, tuple[float | None, str | None]]:
    """Batch-fetch season ERA for many pitchers. Returns id -> (era, throws)."""
    ids = sorted({int(x) for x in pitcher_ids if x})
    out: dict[int, tuple[float | None, str | None]] = {}
    if not ids:
        return out
    chunk_size = 45
    hydrate = f"stats(group=[pitching],type=[season],season={season})"
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        try:
            j = get_json(
                "/people",
                params={
                    "personIds": ",".join(str(x) for x in chunk),
                    "hydrate": hydrate,
                },
            )
        except requests.RequestException as exc:
            logger.warning("mlb_client: batch people hydrate failed: %s", exc)
            continue
        for p in j.get("people") or []:
            pid = p.get("id")
            if pid is None:
                continue
            throws = None
            ph = p.get("pitchHand") or {}
            if isinstance(ph, dict):
                throws = ph.get("code")
            st = None
            for sb in p.get("stats") or []:
                for sp in sb.get("splits") or []:
                    st = sp.get("stat")
                    if st and st.get("era") is not None:
                        break
                if st:
                    break
            era_f: float | None = None
            if st:
                era_f = _float_or_none(st.get("era"))
            out[int(pid)] = (era_f, throws)
    return out


def _wind_parse(wind_raw: str | None) -> tuple[float | None, str | None]:
    if not wind_raw or not str(wind_raw).strip():
        return None, None
    s = str(wind_raw).strip()
    m = re.match(r"^(\d+)\s*mph,\s*(.+)$", s, re.I)
    if m:
        try:
            return float(m.group(1)), m.group(2).strip()
        except ValueError:
            pass
    return None, s


def _weather_block(w: dict | None) -> dict | None:
    if not w:
        return None
    temp_raw = w.get("temp")
    temp_f: float | None = None
    try:
        if temp_raw is not None and str(temp_raw).strip():
            temp_f = float(str(temp_raw))
    except ValueError:
        temp_f = None
    spd, direc = _wind_parse(w.get("wind"))
    return {
        "condition": w.get("condition"),
        "temp_f": temp_f,
        "wind_speed": spd,
        "wind_dir": direc,
    }


def _normalize_game_row(raw: dict, era_map: dict[int, tuple[float | None, str | None]]) -> dict:
    game_id = raw.get("gamePk")
    gd = raw.get("gameDate") or ""
    game_time_et = None
    if gd:
        try:
            if gd.endswith("Z"):
                dt = datetime.fromisoformat(gd.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(gd)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            game_time_et = dt.astimezone(ZoneInfo("America/New_York")).isoformat()
        except ValueError:
            game_time_et = gd

    venue = raw.get("venue") or {}
    status = (raw.get("status") or {}).get("detailedState")
    teams = raw.get("teams") or {}

    def side_info(side: str) -> dict:
        block = teams.get(side) or {}
        t = block.get("team") or {}
        return {
            "id": t.get("id"),
            "name": t.get("name"),
            "abbr": t.get("abbreviation"),
        }

    def pitcher_info(side: str) -> dict | None:
        block = teams.get(side) or {}
        pp = block.get("probablePitcher")
        if not pp or not pp.get("id"):
            return None
        pid = int(pp["id"])
        era, throws = era_map.get(pid, (None, None))
        if throws is None:
            ph = pp.get("pitchHand") or {}
            if isinstance(ph, dict):
                throws = ph.get("code")
        return {
            "id": pid,
            "name": pp.get("fullName"),
            "throws": throws,
            "era": era,
        }

    series_game_number = raw.get("seriesGameNumber")
    if series_game_number is None:
        ls = raw.get("linescore") or {}
        series_game_number = ls.get("seriesNumber")

    return {
        "game_id": game_id,
        "game_time_et": game_time_et,
        "venue_name": venue.get("name"),
        "status": status,
        "home": side_info("home"),
        "away": side_info("away"),
        "home_pitcher": pitcher_info("home"),
        "away_pitcher": pitcher_info("away"),
        "weather": _weather_block(raw.get("weather")),
        "series_game_number": series_game_number,
    }


def get_todays_games(date_str: str | None = None) -> list[dict]:
    """
    Today's MLB games with schedule hydrate. Cached 30 minutes in
    ``data/games_cache.json`` (under ``STATE_DIRECTORY`` when set).
    """
    if date_str:
        d = date_str
    else:
        d = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    cache_all = _load_games_cache_file()
    entry = cache_all.get(d)
    now = time.time()
    if isinstance(entry, dict):
        ts = float(entry.get("cached_at", 0))
        if now - ts < _CACHE_TTL_SEC and isinstance(entry.get("games"), list):
            return list(entry["games"])

    try:
        sched = get_json(
            "/schedule",
            params={
                "sportId": 1,
                "date": d,
                "hydrate": _GAME_HYDRATE,
            },
        )
    except requests.RequestException as exc:
        logger.error("mlb_client: schedule fetch failed: %s", exc)
        if isinstance(entry, dict) and isinstance(entry.get("games"), list):
            logger.warning("mlb_client: returning stale cache for %s", d)
            return list(entry["games"])
        return []

    games_raw: list[dict] = []
    for bucket in sched.get("dates") or []:
        games_raw.extend(bucket.get("games") or [])

    pitcher_ids: list[int] = []
    for g in games_raw:
        for side in ("home", "away"):
            pp = ((g.get("teams") or {}).get(side) or {}).get("probablePitcher")
            if pp and pp.get("id"):
                pitcher_ids.append(int(pp["id"]))

    season = _current_season_year(
        datetime.strptime(d, "%Y-%m-%d").date() if len(d) == 10 else date.today()
    )
    era_map = _fetch_pitchers_season_era_map(pitcher_ids, season)
    normalized = [_normalize_game_row(g, era_map) for g in games_raw]

    cache_all[d] = {"cached_at": now, "games": normalized}
    _save_games_cache_file(cache_all)
    return normalized


def _game_score_from_stat(st: dict) -> float | None:
    """Bill James Game Score (simplified from single-game pitching stat)."""
    try:
        outs = int(st.get("outs") or 0)
        ip = outs / 3.0
        hits = int(st.get("hits") or 0)
        er = int(st.get("earnedRuns") or 0)
        ur = int(st.get("runs") or 0) - er
        if ur < 0:
            ur = 0
        bb = int(st.get("baseOnBalls") or 0) + int(st.get("intentionalWalks") or 0)
        k = int(st.get("strikeOuts") or 0)
    except (TypeError, ValueError):
        return None
    if outs <= 0 and not st.get("inningsPitched"):
        return None
    score = 50.0
    score += outs
    score += 2 * max(0, int(ip) - 4)
    score += k
    score -= 2 * hits
    score -= 4 * er
    score -= 2 * ur
    score -= bb
    return round(score, 1)


def get_pitcher_stats(pitcher_id: int, last_n_days: int = 30) -> dict:
    """
    Season + game log (rolling window for trend). ``vsTeam`` MLB endpoint is
    unreliable; opponent-derived stats use ``get_pitcher_vs_team``.
    """
    season = _current_season_year()
    path = f"/people/{int(pitcher_id)}/stats"
    params = [("stats", "season"), ("stats", "gameLog"), ("group", "pitching"), ("season", season)]
    try:
        j = get_json(path, params=params)
    except requests.RequestException as exc:
        logger.warning("mlb_client: pitcher %s stats failed: %s", str(pitcher_id), exc)
        return _empty_pitcher_stats()

    season_st: dict | None = None
    log_splits: list[dict] = []
    for block in j.get("stats") or []:
        disp = (block.get("type") or {}).get("displayName") or ""
        splits = block.get("splits") or []
        if disp == "season" and splits:
            season_st = (splits[0].get("stat") or {}) if splits else None
        if disp == "gameLog":
            log_splits = list(splits)

    if not season_st:
        return _empty_pitcher_stats()

    ip_outs = _parse_ip_to_outs(season_st.get("inningsPitched"))
    ip = _outs_to_ip_float(ip_outs) if ip_outs else 0.0
    k = float(season_st.get("strikeOuts") or 0)
    bb = float(season_st.get("baseOnBalls") or 0) + float(
        season_st.get("intentionalWalks") or 0
    )
    hr = float(season_st.get("homeRuns") or 0)
    k9 = round((k * 9) / ip, 2) if ip > 0 else None
    bb9 = round((bb * 9) / ip, 2) if ip > 0 else None
    hr9 = round((hr * 9) / ip, 2) if ip > 0 else None

    hbp = float(season_st.get("hitByPitch") or 0)
    fip: float | None = None
    if ip > 0:
        c_fip = 3.2
        fip = round((13 * hr + 3 * (bb + hbp) - 2 * k) / ip + c_fip, 2)

    era_f = _float_or_none(season_st.get("era"))
    whip_f = _float_or_none(season_st.get("whip"))

    cutoff = date.today() - timedelta(days=last_n_days)
    recent_logs: list[dict] = []
    for sp in log_splits:
        ds = sp.get("date")
        if not ds:
            continue
        try:
            gd = datetime.strptime(str(ds)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if gd >= cutoff:
            recent_logs.append(sp)
    recent_logs.sort(key=lambda x: str(x.get("date") or ""), reverse=True)

    last_5_scores: list[float] = []
    days_since_last_start: int | None = None
    starts: list[dict] = [
        sp
        for sp in log_splits
        if int((sp.get("stat") or {}).get("gamesStarted") or 0) >= 1
    ]
    starts.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    if starts:
        try:
            last_start = datetime.strptime(str(starts[0].get("date"))[:10], "%Y-%m-%d").date()
            days_since_last_start = (date.today() - last_start).days
        except ValueError:
            days_since_last_start = None

    for sp in recent_logs[:5]:
        gs = _game_score_from_stat(sp.get("stat") or {})
        if gs is not None:
            last_5_scores.append(gs)

    trend = _pitcher_trend_from_logs(log_splits[: min(12, len(log_splits))])

    return {
        "era": era_f,
        "whip": whip_f,
        "fip": fip,
        "k_per_9": k9,
        "bb_per_9": bb9,
        "hr_per_9": hr9,
        "ip": ip,
        "avg_against": _parse_avg(season_st.get("avg")),
        "last_5_game_scores": last_5_scores,
        "days_since_last_start": days_since_last_start,
        "trend": trend,
    }


def _empty_pitcher_stats() -> dict:
    return {
        "era": None,
        "whip": None,
        "fip": None,
        "k_per_9": None,
        "bb_per_9": None,
        "hr_per_9": None,
        "ip": 0.0,
        "avg_against": None,
        "last_5_game_scores": [],
        "days_since_last_start": None,
        "trend": "stable",
    }


def _pitcher_trend_from_logs(log_splits: list[dict]) -> Literal["improving", "declining", "stable"]:
    """Heuristic: compare mean game score of newest 3 vs next 3 outings."""
    scores: list[float] = []
    for sp in log_splits[:9]:
        g = _game_score_from_stat(sp.get("stat") or {})
        if g is not None:
            scores.append(g)
    if len(scores) < 4:
        return "stable"
    a = sum(scores[:3]) / 3
    b = sum(scores[3:6]) / 3
    if a - b > 3:
        return "improving"
    if b - a > 3:
        return "declining"
    return "stable"


def get_pitcher_vs_team(pitcher_id: int, team_id: int) -> dict:
    """
    Career/season line vs ``team_id``.

    Tries official ``stats=vsTeam`` first; if empty (common), aggregates *gameLog*
    for the current season vs that opponent.
    """
    season = _current_season_year()
    path = f"/people/{int(pitcher_id)}/stats"
    params = {
        "stats": "vsTeam",
        "group": "pitching",
        "season": season,
        "sportId": 1,
        "opposingTeamId": int(team_id),
    }
    empty = {"era": None, "avg_against": None, "ip": 0.0, "games": 0}

    try:
        j = get_json(path, params=params)
    except requests.RequestException as exc:
        logger.debug("mlb_client: vsTeam request failed for %s vs %s: %s", pitcher_id, team_id, exc)
        j = None

    if j and j.get("stats"):
        for block in j.get("stats") or []:
            splits = block.get("splits") or []
            if not splits:
                continue
            st = splits[0].get("stat") or {}
            try:
                games = int(st.get("gamesPlayed") or len(splits))
            except (TypeError, ValueError):
                games = len(splits)
            ip = _outs_to_ip_float(_parse_ip_to_outs(st.get("inningsPitched")))
            return {
                "era": _float_or_none(st.get("era")),
                "avg_against": _parse_avg(st.get("avg")),
                "ip": ip,
                "games": games,
            }

    # Fallback: sum gameLog rows where opponent.id matches
    params_gl = {"stats": "gameLog", "group": "pitching", "season": season}
    try:
        j = get_json(path, params=params_gl)
    except requests.RequestException as exc:
        logger.debug("mlb_client: pitcher %s gameLog for vs_team: %s", str(pitcher_id), exc)
        return dict(empty)

    splits: list[dict] = []
    for block in j.get("stats") or []:
        if (block.get("type") or {}).get("displayName") == "gameLog":
            splits = block.get("splits") or []
            break

    h_total = 0
    ab_total = 0
    er_total = 0
    ip_outs_total = 0
    games = 0
    for sp in splits:
        opp = sp.get("opponent") or {}
        if int(opp.get("id") or 0) != int(team_id):
            continue
        st = sp.get("stat") or {}
        games += 1
        try:
            h_total += int(st.get("hits") or 0)
            ab_total += int(st.get("atBats") or 0)
            er_total += int(st.get("earnedRuns") or 0)
        except (TypeError, ValueError):
            pass
        ip_outs_total += _parse_ip_to_outs(st.get("inningsPitched"))

    ip = _outs_to_ip_float(ip_outs_total)
    era: float | None = None
    if ip > 0:
        era = round((er_total * 9) / ip, 2)
    avg_agg: float | None = None
    if ab_total > 0:
        avg_agg = round(h_total / ab_total, 3)
    if games == 0:
        return dict(empty)
    return {"era": era, "avg_against": avg_agg, "ip": ip, "games": games}


def get_team_batting_splits(team_id: int) -> dict:
    """Season hitting + ``statSplits`` sitCodes ``vl`` / ``vr`` (vs LHP / vs RHP)."""
    season = _current_season_year()
    base = {"stats": "season", "group": "hitting", "season": season, "sportId": 1}
    empty = {
        "avg": None,
        "obp": None,
        "slg": None,
        "ops": None,
        "vs_lhp_avg": None,
        "vs_rhp_avg": None,
        "wrc_plus": None,
        "babip": None,
        "k_rate": None,
        "last_10_runs_avg": None,
    }
    try:
        j_season = get_json(f"/teams/{int(team_id)}/stats", params=base)
    except requests.RequestException as exc:
        logger.warning("mlb_client: team %s season hitting failed: %s", str(team_id), exc)
        return dict(empty)

    st_main: dict | None = None
    for block in j_season.get("stats") or []:
        splits = block.get("splits") or []
        if splits:
            st_main = splits[0].get("stat")
            break
    if not st_main:
        return dict(empty)

    def fetch_split(sit: str) -> dict | None:
        try:
            jj = get_json(
                f"/teams/{int(team_id)}/stats",
                params={
                    **base,
                    "stats": "statSplits",
                    "sitCodes": sit,
                },
            )
        except requests.RequestException:
            return None
        for block in jj.get("stats") or []:
            for sp in block.get("splits") or []:
                s = sp.get("stat")
                if s:
                    return s
        return None

    st_vl = fetch_split("vl")
    st_vr = fetch_split("vr")

    pa = float(st_main.get("plateAppearances") or 0)
    so = float(st_main.get("strikeOuts") or 0)
    k_rate = round(so / pa, 4) if pa > 0 else None

    babip_f = _float_or_none(st_main.get("babip"))
    ops_f = _float_or_none(st_main.get("ops"))

    last10 = _last_n_runs_for_team(int(team_id), 10)

    return {
        "avg": _parse_avg(st_main.get("avg")),
        "obp": _float_or_none(st_main.get("obp")),
        "slg": _float_or_none(st_main.get("slg")),
        "ops": ops_f,
        "vs_lhp_avg": _parse_avg(st_vl.get("avg")) if st_vl else None,
        "vs_rhp_avg": _parse_avg(st_vr.get("avg")) if st_vr else None,
        "wrc_plus": None,
        "babip": babip_f,
        "k_rate": k_rate,
        "last_10_runs_avg": last10,
    }


def _last_n_runs_for_team(team_id: int, n: int) -> float | None:
    """Mean runs scored in the team's last *n* completed games."""
    games = _recent_team_games_raw(int(team_id), n + 5)
    runs: list[float] = []
    for g in games:
        st = (g.get("status") or {}).get("abstractGameState")
        if st != "Final":
            continue
        teams = g.get("teams") or {}
        away = (teams.get("away") or {}).get("team") or (teams.get("away") or {})
        home = (teams.get("home") or {}).get("team") or (teams.get("home") or {})
        is_away = int(away.get("id") or 0) == int(team_id)
        side = "away" if is_away else "home"
        sc = int(((teams.get(side) or {}).get("score")) or 0)
        runs.append(float(sc))
        if len(runs) >= n:
            break
    if not runs:
        return None
    return round(sum(runs) / len(runs), 2)


def _recent_team_games_raw(team_id: int, max_games: int) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=45)
    try:
        sched = get_json(
            "/schedule",
            params={
                "sportId": 1,
                "teamId": int(team_id),
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            },
        )
    except requests.RequestException:
        return []
    out: list[dict] = []
    for bucket in reversed(sched.get("dates") or []):
        for g in reversed(bucket.get("games") or []):
            out.append(g)
            if len(out) >= max_games:
                return out
    return out


def get_bullpen_usage(team_id: int, days: int = 7) -> dict:
    """
    Active pitchers with ``gamesStarted`` below threshold treated as relievers;
    scans *gameLog* in the last ``days`` days.
    """
    season = _current_season_year()
    try:
        roster = get_json(
            f"/teams/{int(team_id)}/roster",
            params={"rosterType": "active"},
        )
    except requests.RequestException as exc:
        logger.warning("mlb_client: roster for %s: %s", team_id, exc)
        return _empty_bullpen_usage()

    pitcher_entries = [
        x
        for x in (roster.get("roster") or [])
        if (x.get("position") or {}).get("abbreviation") == "P"
    ]

    pids = [int((x.get("person") or {})["id"]) for x in pitcher_entries if (x.get("person") or {}).get("id")]
    if not pids:
        return _empty_bullpen_usage()

    sp_threshold = 3
    era_by_pid: dict[int, float | None] = {}
    gs_by_pid: dict[int, int] = {}
    chunk_size = 40
    hydrate = f"stats(group=[pitching],type=[season],season={season})"
    for i in range(0, len(pids), chunk_size):
        chunk = pids[i : i + chunk_size]
        try:
            j = get_json(
                "/people",
                params={"personIds": ",".join(str(x) for x in chunk), "hydrate": hydrate},
            )
        except requests.RequestException:
            continue
        for p in j.get("people") or []:
            pid = int(p["id"])
            st = None
            for sb in p.get("stats") or []:
                for sp in sb.get("splits") or []:
                    st = sp.get("stat")
                    if st:
                        break
                if st:
                    break
            if st:
                gs_by_pid[pid] = int(st.get("gamesStarted") or 0)
                era_by_pid[pid] = _float_or_none(st.get("era"))
            else:
                gs_by_pid[pid] = 0
                era_by_pid[pid] = None

    reliever_ids = [pid for pid in pids if gs_by_pid.get(pid, 0) < sp_threshold]
    if not reliever_ids:
        reliever_ids = [pid for pid in pids if gs_by_pid.get(pid, 0) <= 5]

    cutoff = date.today() - timedelta(days=days)
    appearances_by_pid: dict[int, list[date]] = {pid: [] for pid in reliever_ids}
    total_outs = 0
    era_vals: list[float] = []

    for pid in reliever_ids[:20]:
        if era_by_pid.get(pid) is not None:
            era_vals.append(float(era_by_pid[pid]))
        try:
            j = get_json(
                f"/people/{pid}/stats",
                params={"stats": "gameLog", "group": "pitching", "season": season},
            )
        except requests.RequestException:
            continue
        splits: list[dict] = []
        for block in j.get("stats") or []:
            if (block.get("type") or {}).get("displayName") == "gameLog":
                splits = block.get("splits") or []
                break
        for sp in splits:
            if int((sp.get("stat") or {}).get("gamesStarted") or 0) > 0:
                continue
            ds = sp.get("date")
            if not ds:
                continue
            try:
                gd = datetime.strptime(str(ds)[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if gd < cutoff:
                continue
            stt = sp.get("stat") or {}
            gs = int(stt.get("gamesStarted") or 0)
            if gs:
                continue
            outs = int(stt.get("outs") or 0) or _parse_ip_to_outs(stt.get("inningsPitched"))
            total_outs += outs
            appearances_by_pid.setdefault(pid, []).append(gd)

    total_innings = round(total_outs / 3, 2)

    relievers_2_consecutive = 0
    yesterday = date.today() - timedelta(days=1)
    for _pid, dayslist in appearances_by_pid.items():
        if not dayslist:
            continue
        uniq_dates = sorted(set(dayslist))
        for i in range(len(uniq_dates) - 1):
            if (uniq_dates[i + 1] - uniq_dates[i]).days == 1:
                relievers_2_consecutive += 1
                break

    worked_yesterday = sum(
        1 for ds in appearances_by_pid.values() if yesterday in ds
    )
    closer_available = worked_yesterday <= 6
    high_leverage_available = relievers_2_consecutive < max(1, len(reliever_ids) // 3)

    avg_era = None
    if era_vals:
        avg_era = round(sum(era_vals) / len(era_vals), 2)

    return {
        "total_innings": total_innings,
        "relievers_used_2_consecutive": relievers_2_consecutive,
        "closer_available": closer_available,
        "high_leverage_available": high_leverage_available,
        "avg_reliever_era": avg_era,
    }


def _empty_bullpen_usage() -> dict:
    return {
        "total_innings": 0.0,
        "relievers_used_2_consecutive": 0,
        "closer_available": True,
        "high_leverage_available": True,
        "avg_reliever_era": None,
    }


def get_team_last_n_games(team_id: int, n: int = 10) -> dict:
    """Wins, losses, run averages, and home/away W-L over last *n* finals."""
    games = _recent_team_games_raw(int(team_id), n * 2 + 10)
    finals: list[dict] = []
    for g in games:
        if (g.get("status") or {}).get("abstractGameState") == "Final":
            finals.append(g)
        if len(finals) >= n:
            break

    wins = losses = 0
    rs: list[int] = []
    ra: list[int] = []
    home_w = home_l = away_w = away_l = 0

    for g in finals:
        teams = g.get("teams") or {}
        away = teams.get("away") or {}
        home = teams.get("home") or {}
        tid = int(team_id)
        is_away = int((away.get("team") or {}).get("id") or 0) == tid
        my = away if is_away else home
        opp = home if is_away else away
        my_r = int(my.get("score") or 0)
        op_r = int(opp.get("score") or 0)
        rs.append(my_r)
        ra.append(op_r)
        ww = bool(my.get("isWinner") is True)
        if ww:
            wins += 1
            if is_away:
                away_w += 1
            else:
                home_w += 1
        else:
            losses += 1
            if is_away:
                away_l += 1
            else:
                home_l += 1

    if not finals:
        return {
            "wins": 0,
            "losses": 0,
            "avg_runs_scored": None,
            "avg_runs_allowed": None,
            "run_differential": None,
            "home_record": "0-0",
            "away_record": "0-0",
        }

    trs = sum(rs) / len(rs)
    tra = sum(ra) / len(ra)
    return {
        "wins": wins,
        "losses": losses,
        "avg_runs_scored": round(trs, 2),
        "avg_runs_allowed": round(tra, 2),
        "run_differential": round(trs - tra, 2),
        "home_record": f"{home_w}-{home_l}",
        "away_record": f"{away_w}-{away_l}",
    }


def get_umpire_for_game(game_id: int) -> str | None:
    """
    Home-plate umpire. ``/linescore`` does not include officiating crews; we use
    ``/game/{{id}}/boxscore`` (with a single ``linescore`` attempt for compatibility).
    """
    gid = int(game_id)
    try:
        ls = get_json(f"/game/{gid}/linescore")
        officials = ls.get("officials")
        if isinstance(officials, list) and officials:
            for o in officials:
                if (o.get("officialType") or "").lower().replace(" ", "") in (
                    "homeplate",
                    "homeplateumpire",
                ):
                    off = o.get("official") or {}
                    return off.get("fullName")
    except requests.RequestException:
        pass

    try:
        bx = get_json(f"/game/{gid}/boxscore")
    except requests.RequestException:
        return None
    for o in bx.get("officials") or []:
        if (o.get("officialType") or "") == "Home Plate":
            off = o.get("official") or {}
            return off.get("fullName")
    return None
