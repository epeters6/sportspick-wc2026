"""
Fetch football match statistics from ESPN's public API (no key required).

Used to settle props: team shots/tackles/corners/cards, 1H goals, scorers.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary"

# Map our canonical names → ESPN displayName variants
ESPN_TEAM_ALIASES: dict[str, set[str]] = {
    "USA": {"usa", "united states", "us"},
    "South Korea": {"south korea", "korea republic", "korea"},
    "Côte d'Ivoire": {"ivory coast", "cote d'ivoire", "côte d'ivoire"},
    "Curaçao": {"curacao", "curaçao"},
    "Bosnia & Herzegovina": {"bosnia", "bosnia and herzegovina"},
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9\s]", "", s.lower()).strip()


def _teams_match_db(home: str, away: str, espn_names: set[str]) -> bool:
    norms = {_norm(n) for n in espn_names}
    for db_name in (home, away):
        n = _norm(db_name)
        if n not in norms:
            found = False
            for alias in ESPN_TEAM_ALIASES.get(db_name, set()):
                if _norm(alias) in norms:
                    found = True
                    break
            if not found:
                # Last-word nickname: "Senegal" in "Senegal"
                if not any(n.split()[-1] in x or x.split()[-1] in n for x in norms):
                    return False
    return True


def _stat_value(statistics: list[dict], name: str) -> float | None:
    for s in statistics or []:
        if s.get("name") == name:
            try:
                return float(s.get("displayValue", 0))
            except (TypeError, ValueError):
                return None
    return None


ESPN_PLAYER_STAT_MAP: dict[str, str] = {
    "totalShots": "shots",
    "shotsOnTarget": "shots_on_target",
    "totalGoals": "goals",
    "goalAssists": "assists",
    "totalTackles": "tackles",
    "yellowCards": "yellow_cards",
    "redCards": "red_cards",
}


def _safe_num(val: Any) -> float | int | None:
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return None


def _parse_roster_players(data: dict) -> dict[str, dict[str, Any]]:
    """Parse per-player stats from ESPN summary rosters."""
    players: dict[str, dict[str, Any]] = {}
    for roster_block in data.get("rosters") or []:
        for p in roster_block.get("roster") or []:
            name = (p.get("athlete") or {}).get("displayName")
            if not name:
                continue
            stat_dict = {
                s.get("name"): s.get("displayValue")
                for s in (p.get("stats") or [])
                if s.get("name")
            }
            entry: dict[str, Any] = {}
            for espn_name, our_key in ESPN_PLAYER_STAT_MAP.items():
                if espn_name not in stat_dict:
                    continue
                val = _safe_num(stat_dict[espn_name])
                if val is not None:
                    entry[our_key] = val
            if entry:
                players[name] = entry
    return players


def _parse_goal_player(text: str) -> str | None:
    """Extract scorer from ESPN goal text."""
    # "Goal! ... Stephen Eustaquio (Canada) right footed..."
    m = re.search(r"Goal![^.]*?\.\s*([^(]+?)\s*\(", text or "")
    if m:
        return m.group(1).strip()
    m = re.search(r"([A-Z][\w\s'.-]+?)\s*\(", text or "")
    return m.group(1).strip() if m else None


def _parse_assist(text: str) -> str | None:
    m = re.search(r"assist(?:ed)?\s+by\s+([A-Z][\w\s'.-]+)", text or "", re.I)
    return m.group(1).strip() if m else None


def parse_espn_summary(data: dict, *, home_team: str, away_team: str) -> dict[str, Any]:
    """Convert ESPN summary JSON → our match_stats format."""
    box = data.get("boxscore") or {}
    teams = box.get("teams") or []
    home_side: dict[str, Any] = {}
    away_side: dict[str, Any] = {}

    for t in teams:
        side = "home" if t.get("homeAway") == "home" else "away"
        stats = t.get("statistics") or []
        block = {
            "goals": _stat_value(stats, "goals") or 0,
            "shots": _stat_value(stats, "totalShots"),
            "shots_on_target": _stat_value(stats, "shotsOnTarget"),
            "corners": _stat_value(stats, "wonCorners"),
            "yellow_cards": _stat_value(stats, "yellowCards"),
            "red_cards": _stat_value(stats, "redCards"),
            "tackles": _stat_value(stats, "totalTackles"),
            "name": (t.get("team") or {}).get("displayName"),
        }
        if side == "home":
            home_side = block
        else:
            away_side = block

    # Half-time from header linescores
    header = data.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    half_home = half_away = None
    for c in comp.get("competitors") or []:
        lines = c.get("linescores") or []
        if len(lines) >= 1:
            try:
                val = int(lines[0].get("displayValue", 0))
            except (TypeError, ValueError):
                val = 0
            if c.get("homeAway") == "home":
                half_home = val
            else:
                half_away = val

    # Override goals from final score if stats missing
    for c in comp.get("competitors") or []:
        try:
            g = int(c.get("score", 0))
        except (TypeError, ValueError):
            continue
        if c.get("homeAway") == "home":
            home_side["goals"] = g
        else:
            away_side["goals"] = g

    scorers: list[str] = []
    assists: list[str] = []
    for ev in data.get("keyEvents") or []:
        if not ev.get("scoringPlay"):
            continue
        text = ev.get("text") or ""
        player = None
        parts = ev.get("participants") or []
        if parts:
            player = (parts[0].get("athlete") or {}).get("displayName")
        if not player:
            player = _parse_goal_player(text)
        if player:
            scorers.append(player)
        ast = _parse_assist(text)
        if ast:
            assists.append(ast)

    return {
        "source": "espn",
        "half": {"home": half_home, "away": half_away},
        "team": {"home": home_side, "away": away_side},
        "scorers": scorers,
        "assists": assists,
        "players": _parse_roster_players(data),
    }


async def find_espn_event_id(
    client: httpx.AsyncClient,
    home_team: str,
    away_team: str,
    scheduled_at: str | None,
) -> str | None:
    if not scheduled_at:
        return None
    try:
        dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    except Exception:
        return None
    date_param = dt.strftime("%Y%m%d")
    r = await client.get(ESPN_SCOREBOARD, params={"dates": date_param})
    r.raise_for_status()
    for event in r.json().get("events") or []:
        comps = (event.get("competitions") or [{}])[0]
        names = {
            (c.get("team") or {}).get("displayName", "")
            for c in comps.get("competitors") or []
        }
        if _teams_match_db(home_team, away_team, names):
            return str(event.get("id"))
    return None


async def fetch_espn_match_stats(
    home_team: str,
    away_team: str,
    scheduled_at: str | None,
) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=20) as client:
        eid = await find_espn_event_id(client, home_team, away_team, scheduled_at)
        if not eid:
            logger.debug(f"ESPN: no event for {home_team} vs {away_team}")
            return None
        r = await client.get(ESPN_SUMMARY, params={"event": eid})
        r.raise_for_status()
        stats = parse_espn_summary(r.json(), home_team=home_team, away_team=away_team)
        stats["espn_event_id"] = eid
        return stats


async def sync_football_stats_for_match(match: dict) -> dict[str, Any] | None:
    if not match.get("is_final"):
        return None
    if match.get("sport") not in ("football", None):
        return None
    return await fetch_espn_match_stats(
        match.get("home_team") or "",
        match.get("away_team") or "",
        match.get("scheduled_at"),
    )
