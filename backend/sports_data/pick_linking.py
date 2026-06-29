"""Shared heuristics for linking picks to matches."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from backend.scrapers.pick_extractor import TEAM_ALIASES, _resolve_team_token
from backend.sports_data.mlb_fetcher import MLB_TEAM_ALIASES, canonicalise_mlb_team

_PROP_TYPES = frozenset({
    "total_goals", "total_runs", "btts", "corners", "cards", "shots", "spread",
    "team_shots", "team_tackles", "team_total_goals", "team_total_runs",
    "first_half_goals", "first_five_runs",
    "player_scorer", "player_assists", "player_shots", "player_strikeouts",
    "player_goals", "player_tackles", "player_hits", "player_rbis",
})

_VS_RE = re.compile(
    r"([A-Za-z][\w\s'.-]{2,35}?)\s+vs\.?\s+([A-Za-z][\w\s'.-]{2,35})",
    re.IGNORECASE,
)

_INVALID_SUBJECTS = frozenset({"match", "1h", "f5", "with", "the", "and"})


def normalize_player_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    s = re.sub(r"\s+to\s+go$", "", s, flags=re.I)
    return s.strip()


def parse_dt(dt: str | None) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except Exception:
        return None


def build_match_index(matches: list[dict]) -> tuple[dict[str, list[dict]], dict[str, str]]:
    alias_to_canonical: dict[str, str] = {}
    for alias, canonical in TEAM_ALIASES.items():
        alias_to_canonical[alias] = canonical
    for alias, canonical in MLB_TEAM_ALIASES.items():
        alias_to_canonical[alias] = canonical

    for m in matches:
        for team in (m.get("home_team"), m.get("away_team")):
            if team:
                alias_to_canonical[team.lower()] = team

    by_team: dict[str, list[dict]] = {}
    for m in matches:
        for team in (m.get("home_team"), m.get("away_team")):
            if not team:
                continue
            by_team.setdefault(team, []).append(m)
            for alias, canonical in TEAM_ALIASES.items():
                if canonical == team:
                    by_team.setdefault(alias.title(), []).append(m)
            for alias, canonical in MLB_TEAM_ALIASES.items():
                if canonical == team:
                    by_team.setdefault(alias.title(), []).append(m)
                    by_team.setdefault(alias, []).append(m)
    return by_team, alias_to_canonical


def _team_in_text(team: str, text_lower: str) -> bool:
    tl = (team or "").lower().strip()
    if not tl or not text_lower:
        return False
    if tl in text_lower:
        return True
    for n in range(min(len(tl), 14), 5, -1):
        if tl[:n] in text_lower:
            return True
    last = tl.split()[-1] if tl else ""
    return len(last) > 3 and last in text_lower


def resolve_team_from_subject(subject: str | None) -> str | None:
    s = (subject or "").strip()
    if not s or s.lower() in _INVALID_SUBJECTS or " vs " in s.lower():
        return None
    if len(s) < 3:
        return None
    return _resolve_team_token(s) or canonicalise_mlb_team(s)


def matches_in_text(text: str, matches: list[dict]) -> list[dict]:
    """Find fixtures whose teams appear in post text."""
    text_lower = (text or "").lower()
    if not text_lower:
        return []

    found: list[dict] = []
    seen: set[str] = set()

    def _add(m: dict) -> None:
        mid = m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            found.append(m)

    for m in matches:
        home = m.get("home_team") or ""
        away = m.get("away_team") or ""
        if home and away and _team_in_text(home, text_lower) and _team_in_text(away, text_lower):
            _add(m)

    for vm in _VS_RE.finditer(text or ""):
        t1 = resolve_team_from_subject(vm.group(1).strip()) or vm.group(1).strip()
        t2 = resolve_team_from_subject(vm.group(2).strip()) or vm.group(2).strip()
        for m in matches:
            home, away = m.get("home_team") or "", m.get("away_team") or ""
            teams = {_norm_team(home), _norm_team(away)}
            if _norm_team(t1) in teams and _norm_team(t2) in teams:
                _add(m)

    return found


def _filter_by_vs_phrase(text: str, candidates: list[dict]) -> list[dict]:
    """When text names a fixture explicitly, keep only that pairing."""
    vm = _VS_RE.search(text or "")
    if not vm or len(candidates) <= 1:
        return candidates
    t1 = resolve_team_from_subject(vm.group(1).strip()) or vm.group(1).strip()
    t2 = resolve_team_from_subject(vm.group(2).strip()) or vm.group(2).strip()
    n1, n2 = _norm_team(t1), _norm_team(t2)
    filtered = [
        m for m in candidates
        if {_norm_team(m.get("home_team") or ""), _norm_team(m.get("away_team") or "")} == {n1, n2}
    ]
    return filtered or candidates


def _norm_team(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def infer_match_candidates(
    pick: dict,
    matches: list[dict],
    by_team: dict[str, list[dict]],
    alias_to_canonical: dict[str, str],
) -> list[dict]:
    team = pick.get("predicted_winner") or ""
    bet_type = pick.get("bet_type") or "moneyline"
    text = pick.get("raw_text") or ""
    subject = pick.get("bet_subject")

    is_player = bet_type.startswith("player_")
    is_prop = bet_type in _PROP_TYPES or is_player or team.lower() in ("over", "under", "yes", "no")

    if is_prop or team.lower() == "draw":
        candidates = matches_in_text(text, matches)
        if not candidates and subject:
            team_c = resolve_team_from_subject(normalize_player_subject(subject))
            if team_c:
                candidates = by_team.get(team_c, [])
        if not candidates and is_player and team and team.lower() not in ("over", "under"):
            team_c = resolve_team_from_subject(normalize_player_subject(team))
            if team_c:
                candidates = by_team.get(team_c, [])
        return _filter_by_vs_phrase(text, candidates)

    candidates = by_team.get(team, [])
    if not candidates and team:
        canonical = alias_to_canonical.get(team.lower())
        if canonical:
            candidates = by_team.get(canonical, [])
    return candidates


def pick_best_match(candidates: list[dict], posted_at: str | None, text: str | None = None) -> dict | None:
    if not candidates:
        return None
    if text:
        candidates = _filter_by_vs_phrase(text, candidates)
    posted = parse_dt(posted_at)

    def _sort_key(m: dict):
        sched = parse_dt(m.get("scheduled_at"))
        if sched is None:
            return (2, 0)
        if posted is not None:
            delta = (sched - posted).total_seconds()
            return (0, delta) if delta >= 0 else (1, -delta)
        return (0, abs(sched.timestamp()))

    return sorted(candidates, key=_sort_key)[0]
