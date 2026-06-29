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
