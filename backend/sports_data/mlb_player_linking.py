"""Link MLB player props to games when post text lacks an explicit matchup."""
from __future__ import annotations

import re
from typing import Any

import httpx
from loguru import logger

from backend.sports_data.mlb_fetcher import canonicalise_mlb_team
from backend.sports_data.pick_linking import normalize_player_subject, parse_dt, pick_best_match

MLB_PEOPLE_SEARCH = "https://statsapi.mlb.com/api/v1/people/search"
MLB_PERSON = "https://statsapi.mlb.com/api/v1/people/{person_id}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _name_matches(pick_name: str, candidate: str) -> bool:
    from backend.sports_data.bet_settlement import _name_matches as match
    return match(pick_name, candidate)


def _team_matches_db(team: str, home: str, away: str) -> bool:
    canon = canonicalise_mlb_team(team) or team
    for side in (home, away):
        if not side:
            continue
        if canon == side or _norm(canon) == _norm(side):
            return True
        if _norm(canon.split()[-1]) == _norm(side.split()[-1]):
            return True
    return False


def lookup_player_team(player_name: str) -> str | None:
    name = normalize_player_subject(player_name)
    if not name or len(name) < 3:
        return None
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(MLB_PEOPLE_SEARCH, params={"names": name})
            r.raise_for_status()
            people = r.json().get("people") or []
            if not people:
                return None
            pid = people[0]["id"]
            r2 = client.get(
                MLB_PERSON.format(person_id=pid),
                params={"hydrate": "currentTeam"},
            )
            r2.raise_for_status()
            person = (r2.json().get("people") or [{}])[0]
            return (person.get("currentTeam") or {}).get("name")
    except Exception as exc:
        logger.debug(f"MLB player team lookup failed for {name}: {exc}")
        return None


def find_match_by_player_in_stats(
    player_name: str,
    posted_at: str | None,
    matches: list[dict],
) -> dict | None:
    """Match finished games whose box score already lists this player."""
    name = normalize_player_subject(player_name)
    if not name:
        return None
    candidates: list[dict] = []
    for m in matches:
        if not m.get("is_final"):
            continue
        players = (m.get("match_stats") or {}).get("players") or {}
        if not any(_name_matches(name, pname) for pname in players):
            continue
        candidates.append(m)
    return pick_best_match(candidates, posted_at)


def infer_mlb_match_for_player_pick(
    pick: dict,
    mlb_matches: list[dict],
) -> dict | None:
    """Infer MLB fixture for a player prop with no explicit teams in text."""
    bet_type = pick.get("bet_type") or ""
    if not bet_type.startswith("player_"):
        return None

    subject = normalize_player_subject(pick.get("bet_subject"))
    if not subject or subject.lower() in {"with", "the"}:
        subject = normalize_player_subject(pick.get("predicted_winner"))
    if not subject or subject.lower() in {"over", "under", "yes", "no"}:
        subject = normalize_player_subject(pick.get("bet_subject"))
    if not subject:
        return None

    from_stats = find_match_by_player_in_stats(subject, pick.get("posted_at"), mlb_matches)
    if from_stats:
        return from_stats

    team = lookup_player_team(subject)
    if not team:
        return None

    candidates = [
        m for m in mlb_matches
        if _team_matches_db(team, m.get("home_team") or "", m.get("away_team") or "")
    ]
    return pick_best_match(candidates, pick.get("posted_at"), pick.get("raw_text"))
