"""
Market matcher — link a Polymarket market to a row in our `matches` table and
map our consensus pick to the specific tradeable outcome (token).

Polymarket soccer markets come in a few real-world shapes:

  A. Per-match moneyline (what we want), Yes/No outcomes:
       "Will Germany win on 2026-06-20?"        → 1 team + a date
       "Will Brazil beat Argentina?"            → 2 teams
       "Brazil vs Argentina"                    → 2 teams, team-named outcomes
  B. Tournament outright / futures (MUST be excluded — not a per-match bet):
       "Will USA win the 2026 FIFA World Cup?"
       "Will Brazil win the World Cup?"

We classify each market, reject futures, then match on team(s) + date proximity.
Outcome mapping handles both Yes/No markets (pick == question team → "Yes",
pick == opponent → "No") and team-named markets (map by canonical name).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from loguru import logger

from backend.scrapers.pick_extractor import TEAM_ALIASES
from backend.sports_data.mlb_fetcher import MLB_TEAM_ALIASES
from backend.trading.polymarket_client import PolyMarket, Outcome

# Combined alias map: lowercased alias → canonical name (soccer + MLB)
_ALL_ALIASES: dict[str, str] = {}
_ALL_ALIASES.update(TEAM_ALIASES)
_ALL_ALIASES.update(MLB_TEAM_ALIASES)

# Futures / outright phrasing that means "tournament winner", not a single match
_FUTURES_RE = re.compile(
    r"\bwin\s+the\b.*\b(world cup|tournament|title|championship|cup|series|pennant|division)\b"
    r"|\bto\s+win\s+the\b"
    r"|\btop\s+goalscorer\b|\bgolden boot\b|\bto\s+qualify\b|\bto\s+advance\b|\breach\s+the\b",
    re.IGNORECASE,
)

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _canonical(name: str) -> str | None:
    n = (name or "").strip().lower()
    if n in _ALL_ALIASES:
        return _ALL_ALIASES[n]
    for alias, canonical in _ALL_ALIASES.items():
        if alias in n:
            return canonical
    return None


def _teams_in_text(text: str) -> set[str]:
    """Return the set of canonical team names mentioned in a market question."""
    text_lower = (text or "").lower()
    found: set[str] = set()
    for alias, canonical in _ALL_ALIASES.items():
        if len(alias) <= 4:  # short aliases need word boundaries
            if re.search(rf"\b{re.escape(alias)}\b", text_lower):
                found.add(canonical)
        elif alias in text_lower:
            found.add(canonical)
    return found


def _parse_dt(dt: str | None) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except Exception:
        return None


def _date_in_question(text: str) -> datetime | None:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def is_per_match_market(market: PolyMarket) -> bool:
    """True if this looks like a single-match moneyline (not a futures/outright)."""
    q = market.question or ""
    if _FUTURES_RE.search(q):
        return False
    teams = _teams_in_text(q)
    # Per-match if it names ≥2 teams, OR names 1 team AND carries a specific date
    return len(teams) >= 2 or (len(teams) == 1 and _date_in_question(q) is not None)


def match_market_to_db_match(
    market: PolyMarket,
    db_matches: list[dict],
    *,
    max_days_apart: int = 2,
) -> dict | None:
    """
    Find the DB match a Polymarket market refers to.

    db_matches: list of {id, home_team, away_team, scheduled_at}
    Returns the matching DB match dict, or None.
    """
    if not is_per_match_market(market):
        return None

    q_lower = (market.question or "").lower()
    is_draw_market = "draw" in q_lower or " tie" in q_lower or q_lower.endswith(" tie?")

    market_teams = _teams_in_text(market.question)
    if not market_teams:
        # Draw-only markets often omit team names — ok when caller passes one fixture
        if is_draw_market and len(db_matches) == 1:
            return db_matches[0]
        return None

    # Preferred date anchor: explicit date in question, else the market end date
    anchor = _date_in_question(market.question) or _parse_dt(market.end_date)
    if anchor and anchor.tzinfo is not None:
        anchor = anchor.replace(tzinfo=None)

    best: dict | None = None
    best_gap = timedelta(days=max_days_apart + 1)

    for m in db_matches:
        home = _canonical(m.get("home_team", "")) or m.get("home_team")
        away = _canonical(m.get("away_team", "")) or m.get("away_team")
        match_teams = {t for t in (home, away) if t}
        if not match_teams:
            continue

        # All teams named in the market must be teams in this match.
        if not market_teams.issubset(match_teams):
            continue
        # Two-team markets must match both sides exactly.
        if len(market_teams) >= 2 and market_teams != match_teams:
            continue

        sched = _parse_dt(m.get("scheduled_at"))
        if sched and sched.tzinfo is not None:
            sched = sched.replace(tzinfo=None)

        sport = (m.get("sport") or "").lower()
        days_limit = max_days_apart
        if sport == "mlb":
            days_limit = max(days_limit, 14)
        # "Team A vs. Team B" game markets often lack an in-question date; end_date
        # can be days after first pitch — match on teams and closest schedule.
        skip_date_check = (
            len(market_teams) >= 2
            and market_teams == match_teams
            and _date_in_question(market.question) is None
        )

        if skip_date_check:
            if anchor and sched:
                gap = abs(anchor - sched)
                if best is None or gap < best_gap:
                    best_gap = gap
                    best = m
            elif best is None:
                best = m
            continue

        if anchor and sched:
            gap = abs(anchor - sched)
            if gap > timedelta(days=days_limit):
                continue
            if gap < best_gap:
                best_gap = gap
                best = m
        elif best is None:
            best = m

    return best


def map_outcome_to_token(
    market: PolyMarket,
    consensus_winner: str,
    db_match: dict,
) -> Outcome | None:
    """
    Map our consensus pick (canonical team name or 'draw') to the Polymarket
    outcome (token) we'd trade. Returns the Outcome or None.

    Handles:
      - Team-named outcomes  → match by canonical name (incl. explicit 'draw').
      - Yes/No "Will {team} win?" market → only the "Yes" side, and only when our
        pick IS the subject team.

    NOTE — we deliberately do NOT bet the "No" side of a Yes/No win market: on a
    soccer market "No" pays out on the opponent winning OR a draw, which doesn't
    match our moneyline prediction semantics and would be mis-settled on a draw.
    Skipping it is the safe, correct choice. (Two-team / 3-way markets with
    explicit team/draw outcomes are still fully supported below.)
    """
    if not consensus_winner:
        return None
    pick = consensus_winner.strip().lower()
    pick_canon = _canonical(consensus_winner) or consensus_winner

    names = {o.name.strip().lower() for o in market.outcomes}
    is_yes_no = names == {"yes", "no"}

    if is_yes_no:
        q_lower = market.question.lower()

        if (
            "draw" in q_lower or "tie" in q_lower or "end in a tie" in q_lower
            or "finished level" in q_lower
        ):
            if pick == "draw":
                return market.outcome_by_name("Yes")
            return None

        question_teams = _teams_in_text(market.question)
        subject = None
        m = re.search(r"will\s+(.+?)\s+(?:win|beat|advance|qualify)", q_lower)
        if m:
            subject = _canonical(m.group(1))
        if subject is None and len(question_teams) == 1:
            subject = next(iter(question_teams))

        if subject is not None and pick != "draw":
            if pick_canon == subject or _canonical(pick_canon) == subject:
                return market.outcome_by_name("Yes")
        return None

    # Team-named outcomes: map by canonical name / draw
    for outcome in market.outcomes:
        oc = outcome.name.strip().lower()
        if oc == pick:
            return outcome
        if pick == "draw" and oc in ("draw", "tie", "no winner"):
            return outcome
        canon = _canonical(outcome.name)
        if canon and (canon == pick_canon or canon == consensus_winner):
            return outcome
    return None


# ── Prop markets (O/U, BTTS) ─────────────────────────────────────────────────

_TOTAL_RE = re.compile(
    r"\b(?:over|under|o/u|total)\s*(?:/|\s)?\s*(\d+(?:\.\d+)?)\s*(?:goals?|g\b)?",
    re.IGNORECASE,
)
_BTTS_RE = re.compile(
    r"\b(?:both teams (?:to )?score|btts|bts)\b",
    re.IGNORECASE,
)
_MLB_OU_RE = re.compile(r":\s*O/U\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
# Partial-game / side markets — not full-game moneylines
_MLB_PARTIAL_RE = re.compile(
    r"\b1st\s+\d+\s+innings?\b|\bspread:\s|extra\s+innings|first\s+inning|"
    r"run\s+scored|go\s+to\s+extra",
    re.IGNORECASE,
)


def outcome_belongs_to_match(winner: str, db_match: dict) -> bool:
    """True when a consensus/pick outcome is valid for this fixture."""
    if not winner:
        return False
    sport = (db_match.get("sport") or "").lower()
    pick = winner.strip().lower()
    if pick == "draw":
        return sport != "mlb"
    if pick in ("over", "under", "yes", "no"):
        return True

    home = db_match.get("home_team") or ""
    away = db_match.get("away_team") or ""
    valid = {
        home.strip().lower(),
        away.strip().lower(),
        (_canonical(home) or "").lower(),
        (_canonical(away) or "").lower(),
    }
    pick_canon = (_canonical(winner) or winner).strip().lower()
    return pick in valid or pick_canon in valid


def is_prop_market(market: PolyMarket) -> bool:
    q = market.question or ""
    if _BTTS_RE.search(q):
        return True
    ql = q.lower()
    if _MLB_OU_RE.search(q):
        return True
    if _MLB_PARTIAL_RE.search(q):
        return True
    if _TOTAL_RE.search(q) and ("goal" in ql or "total" in ql or "o/u" in ql):
        return True
    return False


def _extract_total_line(question: str) -> float | None:
    m = _MLB_OU_RE.search(question or "")
    if m:
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            pass
    m = _TOTAL_RE.search(question or "")
    if m:
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            pass
    m2 = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:goals?|g)\b", question or "", re.I)
    if m2:
        try:
            return float(m2.group(1))
        except (TypeError, ValueError):
            pass
    return None


def match_prop_market_to_db_match(
    market: PolyMarket,
    db_match: dict,
    *,
    bet_type: str,
    bet_line: str | None = None,
) -> bool:
    home = (db_match.get("home_team") or "").lower()
    away = (db_match.get("away_team") or "").lower()
    q = (market.question or "").lower()
    teams_ok = home and away and home in q and away in q
    if not teams_ok and not match_market_to_db_match(market, [db_match]):
        return False

    if bet_type == "btts":
        return bool(_BTTS_RE.search(market.question or ""))

    if bet_type == "total_goals":
        line = _extract_total_line(market.question or "")
        if line is None:
            return "total" in q or "goal" in q or "o/u" in q
        try:
            target = float(bet_line or "2.5")
        except (TypeError, ValueError):
            target = 2.5
        # Skip 1st-inning / partial-game lines when consensus is full-game total
        if "innings" in q or "1st 5" in q:
            return False
        return abs(line - target) < 0.01

    return True


def map_prop_outcome_to_token(
    market: PolyMarket,
    *,
    bet_type: str,
    predicted_winner: str,
    bet_line: str | None,
) -> Outcome | None:
    pick = (predicted_winner or "").strip().lower()
    q = (market.question or "").lower()
    names = {o.name.strip().lower() for o in market.outcomes}
    is_yes_no = names == {"yes", "no"}

    if bet_type == "btts":
        if is_yes_no:
            if pick in ("yes", "y"):
                return market.outcome_by_name("Yes")
            if pick in ("no", "n"):
                return market.outcome_by_name("No")
        return None

    if bet_type == "total_goals":
        line_str = bet_line or "2.5"
        if is_yes_no:
            if pick == "over" and "over" in q:
                return market.outcome_by_name("Yes")
            if pick == "under" and "under" in q:
                return market.outcome_by_name("Yes")
        for o in market.outcomes:
            oc = o.name.strip().lower()
            if pick == "over" and oc.startswith("over") and line_str in oc:
                return o
            if pick == "under" and oc.startswith("under") and line_str in oc:
                return o
        if pick == "over":
            for o in market.outcomes:
                if "over" in o.name.lower():
                    return o
        if pick == "under":
            for o in market.outcomes:
                if "under" in o.name.lower():
                    return o
    return None
