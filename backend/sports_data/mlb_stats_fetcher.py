"""
Fetch MLB box score statistics for prop settlement (F5, team totals, player K/H).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

import httpx
from loguru import logger

MLB_BOXSCORE = "https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
MLB_LINESCORE = "https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore"
MLB_PERSON = "https://statsapi.mlb.com/api/v1/people/{person_id}"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9\s]", "", s.lower()).strip()


def _name_matches(a: str, b: str) -> bool:
    if _norm(a) == _norm(b):
        return True
    al, bl = _norm(a).split(), _norm(b).split()
    return bool(al and bl and al[-1] == bl[-1])


def _game_pk(external_id: str | None) -> str | None:
    if not external_id:
        return None
    m = re.search(r"mlb_(\d+)", external_id)
    return m.group(1) if m else None


def parse_mlb_boxscore(box: dict, linescore: dict) -> dict[str, Any]:
    teams_data: dict[str, dict] = {"home": {}, "away": {}}
    players: dict[str, dict] = {}

    for side in ("home", "away"):
        t = box.get("teams", {}).get(side, {})
        batting = (t.get("teamStats") or {}).get("batting") or {}
        pitching = (t.get("teamStats") or {}).get("pitching") or {}
        runs = batting.get("runs")
        if runs is None:
            runs = (linescore.get("teams") or {}).get(side, {}).get("runs")
        teams_data[side] = {
            "runs": runs,
            "hits": batting.get("hits"),
            "strikeouts": (pitching.get("strikeOuts") or 0) + (batting.get("strikeOuts") or 0),
            "name": (t.get("team") or {}).get("name"),
        }

        for _pid, pdata in (t.get("players") or {}).items():
            person = (pdata.get("person") or {}).get("fullName")
            if not person:
                continue
            bat = (pdata.get("stats") or {}).get("batting") or {}
            pitch = (pdata.get("stats") or {}).get("pitching") or {}
            entry: dict[str, Any] = {}
            if bat.get("hits") is not None:
                entry["hits"] = bat["hits"]
            if bat.get("rbi") is not None:
                entry["rbis"] = bat["rbi"]
            if pitch.get("strikeOuts") is not None:
                entry["strikeouts"] = pitch["strikeOuts"]
            if entry:
                players[person] = entry

    # Innings → first five runs
    innings = linescore.get("innings") or []
    f5_home = f5_away = 0
    for inn in innings:
        if inn.get("num", 99) > 5:
            break
        f5_home += (inn.get("home") or {}).get("runs") or 0
        f5_away += (inn.get("away") or {}).get("runs") or 0

    return {
        "source": "mlb_statsapi",
        "team": teams_data,
        "first_five": {
            "home": f5_home,
            "away": f5_away,
            "total": f5_home + f5_away,
        },
        "innings": {
            "home": [(i.get("home") or {}).get("runs") for i in innings],
            "away": [(i.get("away") or {}).get("runs") for i in innings],
        },
        "players": players,
        "scorers": [],
        "half": {},
    }


async def fetch_pitcher_season_stats(person_id: int | str) -> dict[str, Any] | None:
    """Season pitching line for probable-SP matchup weighting."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            MLB_PERSON.format(person_id=person_id),
            params={"hydrate": "stats(group=[pitching],type=[season])"},
        )
        if r.status_code != 200:
            return None
        people = r.json().get("people") or []
        if not people:
            return None
        stats = (people[0].get("stats") or [])
        if not stats:
            return None
        splits = (stats[0].get("splits") or [])
        if not splits:
            return None
        stat = splits[0].get("stat") or {}
        try:
            era = float(stat.get("era") or 99.0)
        except (TypeError, ValueError):
            era = 99.0
        try:
            whip = float(stat.get("whip") or 9.0)
        except (TypeError, ValueError):
            whip = 9.0
        gs = int(stat.get("gamesStarted") or 0)
        if gs < 1:
            return None
        return {
            "era": era,
            "whip": whip,
            "games_started": gs,
            "wins": int(stat.get("wins") or 0),
            "losses": int(stat.get("losses") or 0),
        }


def sp_matchup_favored_team(match_stats: dict | None) -> str | None:
    """
    Compare probable starter ERAs from match_stats; return favored team name.
    Requires enriched probable_pitchers with era fields.
    """
    pitchers = (match_stats or {}).get("probable_pitchers") or {}
    home = pitchers.get("home") or {}
    away = pitchers.get("away") or {}
    home_era = home.get("era")
    away_era = away.get("era")
    if home_era is None or away_era is None:
        return None
    if abs(home_era - away_era) < 0.35:
        return None
    if home_era < away_era:
        return home.get("team")
    return away.get("team")


async def enrich_probable_pitchers(match_stats: dict | None) -> dict | None:
    """Attach season ERA/WHIP to probable_pitchers entries when missing."""
    if not match_stats:
        return match_stats
    pitchers = dict((match_stats or {}).get("probable_pitchers") or {})
    if not pitchers:
        return match_stats
    changed = False
    for side, pdata in list(pitchers.items()):
        if pdata.get("era") is not None:
            continue
        pid = pdata.get("id")
        if not pid:
            continue
        stats = await fetch_pitcher_season_stats(pid)
        if not stats:
            continue
        pitchers[side] = {**pdata, **stats}
        changed = True
    if not changed:
        return match_stats
    out = dict(match_stats)
    out["probable_pitchers"] = pitchers
    return out


async def enrich_upcoming_mlb_pitcher_stats(limit: int = 40) -> int:
    """Attach season ERA to probable starters on upcoming MLB games."""
    from backend.db import get_db

    db = get_db()
    rows = (
        db.table("matches")
        .select("id, match_stats, is_final")
        .eq("sport", "mlb")
        .eq("is_final", False)
        .limit(limit)
        .execute()
        .data or []
    )
    updated = 0
    for row in rows:
        stats = row.get("match_stats") or {}
        if not stats.get("probable_pitchers"):
            continue
        enriched = await enrich_probable_pitchers(stats)
        if enriched != stats:
            db.table("matches").update({"match_stats": enriched}).eq("id", row["id"]).execute()
            updated += 1
    if updated:
        logger.info(f"MLB: enriched probable pitcher stats on {updated} games")
    return updated


async def fetch_mlb_match_stats(external_id: str | None) -> dict[str, Any] | None:
    pk = _game_pk(external_id)
    if not pk:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        box_r = await client.get(MLB_BOXSCORE.format(game_pk=pk))
        if box_r.status_code == 404:
            return None
        box_r.raise_for_status()
        ls_r = await client.get(MLB_LINESCORE.format(game_pk=pk))
        ls_r.raise_for_status()
        stats = parse_mlb_boxscore(box_r.json(), ls_r.json())
        stats["game_pk"] = pk
        return stats


async def sync_mlb_stats_for_match(match: dict) -> dict[str, Any] | None:
    if not match.get("is_final") or match.get("sport") != "mlb":
        return None
    return await fetch_mlb_match_stats(match.get("external_id"))
