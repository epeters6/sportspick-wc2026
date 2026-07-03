"""
Shared bet grading — resolves any pick/autobet/simulated bet from match + match_stats.

Returns: "correct" | "incorrect" | "void" | None (still pending — match not final or stats missing)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from backend.trading.market_matcher import _canonical
from backend.scrapers.pick_extractor import _resolve_team_token
from backend.sports_data.mlb_fetcher import canonicalise_mlb_team

_INVALID_PLAYER_NAMES = frozenset({"with", "the", "and", "over", "under"})


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9\s]", "", s.lower()).strip()


def _name_matches(pick_name: str, candidate: str) -> bool:
    """Fuzzy player name match (last name, accent-insensitive)."""
    if not pick_name or not candidate:
        return False
    pn, cn = _norm(pick_name), _norm(candidate)
    if pn == cn or pn in cn or cn in pn:
        return True
    pl = pn.split()
    cl = cn.split()
    if pl and cl and pl[-1] == cl[-1]:
        return True
    if pl and cl and pl[0] == cl[0]:
        plast, clast = pl[-1], cl[-1]
        if len(plast) >= 5 and len(clast) >= 5 and plast[:5] == clast[:5]:
            return True
        if len(plast) >= 4 and len(clast) >= 4:
            close = sum(a == b for a, b in zip(plast, clast))
            if close >= max(len(plast), len(clast)) - 1:
                return True
    return False


def _team_side(
    subject: str | None,
    home_team: str,
    away_team: str,
) -> str | None:
    """Return 'home' | 'away' if subject identifies one team."""
    if not subject or subject in ("match", "1h", "f5"):
        return None
    subj = subject.strip()
    if " vs " in subj.lower():
        return None
    for side, team in (("home", home_team), ("away", away_team)):
        if _norm(subj) == _norm(team):
            return side
    resolved = _resolve_team_token(subj) or canonicalise_mlb_team(subj) or subj
    for side, team in (("home", home_team), ("away", away_team)):
        if _norm(resolved) == _norm(team):
            return side
        if _norm(resolved.split()[-1]) == _norm(team.split()[-1]):
            return side
    return None


def _parse_line(bet_line: str | None, default: float = 2.5) -> float:
    try:
        return float(bet_line or default)
    except (TypeError, ValueError):
        return default


def _ou_grade(actual: float, line: float, direction: str) -> str:
    d = (direction or "").lower()
    if actual == line:
        return "void"
    if d == "over":
        return "correct" if actual > line else "incorrect"
    if d == "under":
        return "correct" if actual < line else "incorrect"
    return "incorrect"


def _team_stats(stats: dict, side: str) -> dict:
    return (stats.get("team") or {}).get(side) or {}


def grade_pick(
    *,
    bet_type: str,
    predicted_winner: str,
    bet_line: str | None,
    bet_subject: str | None,
    match: dict,
    match_stats: dict | None,
) -> str | None:
    """
    Grade a single pick. None = leave pending (not final / need stats retry).
    """
    if not match.get("is_final"):
        return None

    bt = bet_type or "moneyline"
    pw = predicted_winner or ""
    stats = match_stats or {}
    home_team = match.get("home_team") or ""
    away_team = match.get("away_team") or ""
    home_score = match.get("home_score")
    away_score = match.get("away_score")

    # ── Moneyline / draw ───────────────────────────────────────────────────
    if bt in ("moneyline", "draw"):
        actual = match.get("winner")
        if actual is None:
            return None
        if bt == "draw" or pw == "draw":
            return "correct" if actual == "draw" else "incorrect"
        pw_c = _canonical(pw) or pw
        act_c = _canonical(actual) or actual
        home_c = _canonical(home_team) or home_team
        away_c = _canonical(away_team) or away_team
        if pw_c == act_c or pw == actual:
            return "correct"
        if pw_c == home_c and act_c == home_c:
            return "correct"
        if pw_c == away_c and act_c == away_c:
            return "correct"
        return "incorrect"

    if home_score is None or away_score is None:
        if bt in ("total_goals", "total_runs", "btts"):
            return None
        if not stats:
            return None

    # ── Match totals ───────────────────────────────────────────────────────
    if bt == "total_goals":
        total = (home_score or 0) + (away_score or 0)
        return _ou_grade(total, _parse_line(bet_line), pw)

    if bt == "total_runs":
        total = (home_score or 0) + (away_score or 0)
        return _ou_grade(total, _parse_line(bet_line, 8.5), pw)

    if bt == "btts":
        both = (home_score or 0) > 0 and (away_score or 0) > 0
        if pw in ("yes", "y"):
            return "correct" if both else "incorrect"
        if pw in ("no", "n"):
            return "correct" if not both else "incorrect"
        return None

    if not stats:
        return None

    half = stats.get("half") or {}
    team_block = stats.get("team") or {}
    players = stats.get("players") or {}
    scorers = stats.get("scorers") or []
    f5 = stats.get("first_five") or {}

    # ── First half ─────────────────────────────────────────────────────────
    if bt == "first_half_goals" or bt.startswith("first_half_"):
        hh = half.get("home")
        ha = half.get("away")
        if hh is None or ha is None:
            return None
        if "goal" in bt or bt == "first_half_goals":
            total = hh + ha
            return _ou_grade(total, _parse_line(bet_line, 1.5), pw)
        return "void"

    if bt == "first_five_runs" or bt.startswith("first_five_"):
        if f5.get("total") is None:
            return None
        return _ou_grade(f5["total"], _parse_line(bet_line, 4.5), pw)

    # ── Match-level props (corners, cards, shots sum) ──────────────────────
    if bt == "corners":
        if team_block.get("home", {}).get("corners") is None:
            return None
        total = team_block["home"].get("corners", 0) + team_block["away"].get("corners", 0)
        return _ou_grade(total, _parse_line(bet_line), pw)

    if bt == "cards":
        if team_block.get("home", {}).get("yellow_cards") is None:
            return None
        total = sum(
            team_block[s].get("yellow_cards", 0) + team_block[s].get("red_cards", 0)
            for s in ("home", "away")
        )
        return _ou_grade(total, _parse_line(bet_line), pw)

    if bt == "shots":
        if team_block.get("home", {}).get("shots") is None:
            return None
        total = team_block["home"].get("shots", 0) + team_block["away"].get("shots", 0)
        return _ou_grade(total, _parse_line(bet_line), pw)

    # ── Team props ─────────────────────────────────────────────────────────
    if bt.startswith("team_"):
        side = _team_side(bet_subject, home_team, away_team)
        if not side:
            return "void"
        ts = _team_stats(stats, side)
        line = _parse_line(bet_line)

        if bt == "team_total_goals":
            val = ts.get("goals")
            if val is None:
                val = home_score if side == "home" else away_score
            return _ou_grade(val or 0, line, pw)

        if bt == "team_total_runs":
            val = ts.get("runs")
            if val is None:
                val = home_score if side == "home" else away_score
            return _ou_grade(val or 0, line, pw)

        stat_key = {
            "team_shots": "shots",
            "team_tackles": "tackles",
            "team_hits": "hits",
            "team_strikeouts": "strikeouts",
        }.get(bt)
        if stat_key and ts.get(stat_key) is not None:
            return _ou_grade(ts[stat_key], line, pw)
        return None

    # ── Player props ───────────────────────────────────────────────────────
    if bt == "player_scorer":
        if not scorers:
            return None
        for scorer in scorers:
            if _name_matches(pw, scorer) or _name_matches(bet_subject or "", scorer):
                return "correct"
        return "incorrect"

    if bt == "player_assists":
        assists = stats.get("assists") or []
        for name in assists:
            if _name_matches(pw, name) or _name_matches(bet_subject or "", name):
                return "correct"
        return "incorrect" if assists else None

    if bt.startswith("player_"):
        from backend.sports_data.pick_linking import normalize_player_subject

        name = normalize_player_subject(bet_subject) or normalize_player_subject(pw)
        if not name or _norm(name) in _INVALID_PLAYER_NAMES:
            return "void"
        pdata = None
        for pname, pstats in players.items():
            if _name_matches(name, pname):
                pdata = pstats
                break
        if not pdata:
            return None

        stat_key = {
            "player_shots": "shots",
            "player_strikeouts": "strikeouts",
            "player_hits": "hits",
            "player_rbis": "rbis",
            "player_goals": "goals",
            "player_tackles": "tackles",
            "player_assists": "assists",
        }.get(bt)
        if not stat_key:
            return "void"
        val = pdata.get(stat_key)
        if val is None:
            return None
        if bt == "player_assists" and val == 0:
            return "incorrect"
        if stat_key == "goals" and bt == "player_goals":
            line = _parse_line(bet_line, 0.5)
            return _ou_grade(val, line, pw)
        line = _parse_line(bet_line, 0.5)
        return _ou_grade(val, line, pw)

    if bt == "spread":
        return "void"

    return None


def pick_won_for_autobet(
    *,
    bet_type: str,
    outcome_name: str,
    bet_line: str | None,
    bet_subject: str | None,
    match: dict,
    match_stats: dict | None,
) -> bool | None:
    """Autobet win/loss. None = cannot settle yet."""
    grade = grade_pick(
        bet_type=bet_type,
        predicted_winner=outcome_name,
        bet_line=bet_line,
        bet_subject=bet_subject,
        match=match,
        match_stats=match_stats,
    )
    if grade is None:
        return None
    if grade == "void":
        return None
    return grade == "correct"
