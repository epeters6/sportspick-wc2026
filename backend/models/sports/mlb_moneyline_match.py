"""Strict MLB moneyline / game-winner matching — separate from pitcher-outs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from backend.trading.market_matcher import _canonical, _teams_in_text

# Kalshi MLB event ticker suffixes use standard abbreviations.
_KALSHI_ABBR_TO_TEAM: dict[str, str] = {
    "NYY": "New York Yankees",
    "NYM": "New York Mets",
    "BOS": "Boston Red Sox",
    "TB": "Tampa Bay Rays",
    "TBR": "Tampa Bay Rays",
    "BAL": "Baltimore Orioles",
    "TOR": "Toronto Blue Jays",
    "CLE": "Cleveland Guardians",
    "CWS": "Chicago White Sox",
    "CHW": "Chicago White Sox",
    "DET": "Detroit Tigers",
    "KC": "Kansas City Royals",
    "KCR": "Kansas City Royals",
    "MIN": "Minnesota Twins",
    "LAA": "Los Angeles Angels",
    "HOU": "Houston Astros",
    "OAK": "Oakland Athletics",
    "ATH": "Oakland Athletics",
    "SEA": "Seattle Mariners",
    "TEX": "Texas Rangers",
    "ATL": "Atlanta Braves",
    "MIA": "Miami Marlins",
    "NYM": "New York Mets",
    "PHI": "Philadelphia Phillies",
    "WSH": "Washington Nationals",
    "WAS": "Washington Nationals",
    "CHC": "Chicago Cubs",
    "CIN": "Cincinnati Reds",
    "MIL": "Milwaukee Brewers",
    "PIT": "Pittsburgh Pirates",
    "STL": "St. Louis Cardinals",
    "ARI": "Arizona Diamondbacks",
    "AZ": "Arizona Diamondbacks",
    "COL": "Colorado Rockies",
    "LAD": "Los Angeles Dodgers",
    "LA": "Los Angeles Dodgers",
    "SD": "San Diego Padres",
    "SDP": "San Diego Padres",
    "SF": "San Francisco Giants",
    "SFG": "San Francisco Giants",
}


@dataclass
class MoneylineMatch:
    market: Any = None
    outcome: Any = None
    selected_team: str = ""
    side: str = ""  # YES | NO (contract side purchased)
    yes_team: Optional[str] = None  # proven YES proposition team when applicable
    model_prob: Optional[float] = None
    rejection_reason: Optional[str] = None


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _same_team(a: str, b: str) -> bool:
    ca = _canonical(a) or a
    cb = _canonical(b) or b
    return _norm(ca) == _norm(cb) or _norm(a) == _norm(b)


def _date_tokens(slate_date: str) -> set[str]:
    tokens: set[str] = set()
    s = (slate_date or "").strip()
    if not s:
        return tokens
    tokens.add(s)
    tokens.add(s.replace("-", ""))
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return tokens
    tokens.add(dt.strftime("%y%m%d"))
    tokens.add(dt.strftime("%m%d"))
    tokens.add(dt.strftime("%m/%d/%Y").lower())
    tokens.add(dt.strftime("%m/%d/%y").lower())
    tokens.add(dt.strftime("%b %d").lower())
    tokens.add(dt.strftime("%b %d").lower().replace(" 0", " "))
    tokens.add(dt.strftime("%B %d").lower())
    # Kalshi: 25JUL21 style
    tokens.add(dt.strftime("%y%b%d").upper())
    tokens.add(dt.strftime("%y%b%d").lower())
    return {t for t in tokens if t}


def _market_corpus(market: Any) -> str:
    parts = [
        getattr(market, "question", "") or "",
        getattr(market, "slug", "") or "",
        str(getattr(market, "end_date", "") or ""),
        str(getattr(market, "market_id", "") or ""),
        str(getattr(market, "gamma_id", "") or ""),
        str(getattr(market, "yes_proposition_team", "") or ""),
    ]
    return " ".join(parts).lower()


def _event_date_matches(market: Any, slate_date: str) -> Optional[str]:
    tokens = _date_tokens(slate_date)
    if not tokens:
        return "DATE_IDENTITY_MISSING"
    corpus = _market_corpus(market)
    end = getattr(market, "end_date", None)
    if end and str(end).startswith(slate_date):
        return None
    for tok in tokens:
        if tok.lower() in corpus:
            return None
    return "DATE_MISMATCH"


def _is_moneyline_question(question: str) -> bool:
    q = (question or "").lower()
    if any(
        tok in q
        for tok in (
            "outs",
            "strikeout",
            "spread",
            "run line",
            "o/u",
            "over/under",
            "1st ",
            "first inning",
            "total runs",
        )
    ):
        return False
    if any(tok in q for tok in ("moneyline", "winner", "to win", "will win", "win?", " vs ", " @ ")):
        return True
    # Team-named binary without prop keywords
    teams = _teams_in_text(question)
    return len(teams) >= 2


def _abbr_for_team(team: str) -> set[str]:
    canon = _canonical(team) or team
    out: set[str] = set()
    for abbr, name in _KALSHI_ABBR_TO_TEAM.items():
        if _same_team(name, canon) or _same_team(name, team):
            out.add(abbr.upper())
    return out


def _kalshi_ticker_has_both_teams(market: Any, home_team: str, away_team: str) -> bool:
    ticker = str(getattr(market, "market_id", "") or getattr(market, "slug", "") or "").upper()
    if not ticker:
        return False
    home_abbrs = _abbr_for_team(home_team)
    away_abbrs = _abbr_for_team(away_team)
    home_ok = any(a in ticker for a in home_abbrs)
    away_ok = any(a in ticker for a in away_abbrs)
    return home_ok and away_ok


def _game_teams_ok(market: Any, home_team: str, away_team: str) -> Optional[str]:
    corpus = _market_corpus(market)
    home_c = _canonical(home_team) or home_team
    away_c = _canonical(away_team) or away_team
    home_ok = _norm(home_c) in _norm(corpus) or _norm(home_team) in _norm(corpus)
    away_ok = _norm(away_c) in _norm(corpus) or _norm(away_team) in _norm(corpus)
    found = _teams_in_text(corpus)
    if home_c in found:
        home_ok = True
    if away_c in found:
        away_ok = True
    if home_ok and away_ok:
        return None
    # Kalshi titles often name only the YES team; both clubs appear in the ticker.
    venue = (getattr(market, "venue", "") or "").lower()
    if venue == "kalshi" and _kalshi_ticker_has_both_teams(market, home_team, away_team):
        yes_team = resolve_kalshi_yes_team(market)
        if yes_team and (
            _same_team(yes_team, home_team) or _same_team(yes_team, away_team)
        ):
            return None
    return "AMBIGUOUS_MLB_GAME_MATCH"


def resolve_kalshi_yes_team(market: Any) -> Optional[str]:
    """
    Prove which team Kalshi YES represents.
    Prefer explicit attribute, then ticker suffix -TEAM, then question subject.
    """
    explicit = getattr(market, "yes_proposition_team", None)
    if explicit:
        return _canonical(str(explicit)) or str(explicit)

    ticker = str(getattr(market, "market_id", "") or getattr(market, "slug", "") or "")
    m = re.search(r"-([A-Z]{2,3})$", ticker.upper())
    if m:
        abbr = m.group(1)
        team = _KALSHI_ABBR_TO_TEAM.get(abbr)
        if team:
            return team

    q = getattr(market, "question", "") or ""
    m2 = re.search(r"will\s+(.+?)\s+win", q, re.IGNORECASE)
    if m2:
        return _canonical(m2.group(1).strip()) or m2.group(1).strip()

    teams = list(_teams_in_text(q))
    if len(teams) == 1:
        return teams[0]
    return None


def _select_polymarket_outcome(
    market: Any, selected_team: str, home_team: str, away_team: str
) -> tuple[Any | None, str, Optional[str]]:
    """Return (outcome, side, rejection). Never use bare Yes without proven subject."""
    names = {(getattr(o, "name", "") or "").strip().lower() for o in (getattr(market, "outcomes", []) or [])}
    is_yes_no = names == {"yes", "no"}
    q = getattr(market, "question", "") or ""

    if is_yes_no:
        subject = None
        m = re.search(r"will\s+(.+?)\s+(?:win|beat)", q, re.IGNORECASE)
        if m:
            subject = _canonical(m.group(1).strip()) or m.group(1).strip()
        if subject is None:
            found = _teams_in_text(q)
            if len(found) == 1:
                subject = next(iter(found))
        if subject is None:
            return None, "", "AMBIGUOUS_MLB_GAME_MATCH"
        yes = next(
            (o for o in market.outcomes if (o.name or "").lower() == "yes"), None
        )
        no = next((o for o in market.outcomes if (o.name or "").lower() == "no"), None)
        if yes is None or not getattr(yes, "token_id", None):
            return None, "", "MISSING_OUTCOME_TOKEN_ID"
        if _same_team(selected_team, subject):
            return yes, "YES", None
        # Buy No only when selected is the other participant and subject is known
        if not (
            (_same_team(selected_team, home_team) or _same_team(selected_team, away_team))
            and (_same_team(subject, home_team) or _same_team(subject, away_team))
        ):
            return None, "", "TEAM_DIRECTION_MISMATCH"
        if no is None or not getattr(no, "token_id", None):
            return None, "", "MISSING_OUTCOME_TOKEN_ID"
        return no, "NO", None

    for o in getattr(market, "outcomes", []) or []:
        if _same_team(getattr(o, "name", "") or "", selected_team):
            if not getattr(o, "token_id", None):
                return None, "", "MISSING_OUTCOME_TOKEN_ID"
            return o, "YES", None
    return None, "", "TEAM_DIRECTION_MISMATCH"


def _select_kalshi_outcome(
    market: Any, selected_team: str, home_team: str, away_team: str
) -> tuple[Any | None, str, Optional[str], Optional[str]]:
    yes_team = resolve_kalshi_yes_team(market)
    if yes_team is None:
        return None, "", "AMBIGUOUS_MLB_GAME_MATCH", None
    if not (
        _same_team(yes_team, home_team) or _same_team(yes_team, away_team)
    ):
        return None, "", "TEAM_DIRECTION_MISMATCH", yes_team

    outcomes = {((getattr(o, "name", "") or "").lower()): o for o in (market.outcomes or [])}
    yes_o = outcomes.get("yes")
    no_o = outcomes.get("no")
    if yes_o is None or no_o is None:
        return None, "", "MISSING_OUTCOME_TOKEN_ID", yes_team

    if _same_team(selected_team, yes_team):
        if not getattr(yes_o, "token_id", None):
            return None, "", "MISSING_OUTCOME_TOKEN_ID", yes_team
        return yes_o, "YES", None, yes_team
    # Selected team is the complement of YES proposition
    if not (
        _same_team(selected_team, home_team) or _same_team(selected_team, away_team)
    ):
        return None, "", "TEAM_DIRECTION_MISMATCH", yes_team
    if not getattr(no_o, "token_id", None):
        return None, "", "MISSING_OUTCOME_TOKEN_ID", yes_team
    return no_o, "NO", None, yes_team


def match_mlb_moneyline_contract(
    *,
    markets: list[Any],
    home_team: str,
    away_team: str,
    slate_date: str,
    selected_team: str,
    venue: Optional[str] = None,
) -> MoneylineMatch:
    """
    Match an MLB game-winner/moneyline market for ``selected_team``.

    Rejects ambiguous, wrong-date, wrong-direction, and unsupported market types.
    Never treats a generic Yes as a team win without proving the proposition.
    """
    if not selected_team:
        return MoneylineMatch(rejection_reason="TEAM_DIRECTION_MISMATCH", selected_team=selected_team)
    if not (
        _same_team(selected_team, home_team) or _same_team(selected_team, away_team)
    ):
        return MoneylineMatch(
            rejection_reason="TEAM_DIRECTION_MISMATCH", selected_team=selected_team
        )

    candidates: list[Any] = []
    saw_unsupported = False
    saw_date_fail = False
    saw_game_fail = False

    for m in markets:
        if venue:
            mv = (getattr(m, "venue", None) or "").lower()
            if mv and mv != venue.lower():
                continue
        q = getattr(m, "question", "") or ""
        if not _is_moneyline_question(q):
            # Track if it looked like MLB game but wrong type
            if home_team and away_team and (
                _norm(home_team) in _norm(q) or _norm(away_team) in _norm(q)
            ):
                saw_unsupported = True
            continue
        date_rej = _event_date_matches(m, slate_date)
        if date_rej:
            saw_date_fail = True
            continue
        game_rej = _game_teams_ok(m, home_team, away_team)
        if game_rej:
            saw_game_fail = True
            continue
        candidates.append(m)

    if not candidates:
        if saw_date_fail:
            return MoneylineMatch(rejection_reason="DATE_MISMATCH", selected_team=selected_team)
        if saw_game_fail:
            return MoneylineMatch(
                rejection_reason="AMBIGUOUS_MLB_GAME_MATCH", selected_team=selected_team
            )
        if saw_unsupported:
            return MoneylineMatch(
                rejection_reason="UNSUPPORTED_MLB_MARKET_TYPE", selected_team=selected_team
            )
        return MoneylineMatch(
            rejection_reason="UNSUPPORTED_MLB_MARKET_TYPE", selected_team=selected_team
        )

    # Distinct market ids → duplicate/ambiguous
    uniq_ids = {str(getattr(m, "market_id", id(m))) for m in candidates}
    if len(uniq_ids) > 1:
        return MoneylineMatch(
            rejection_reason="DUPLICATE_GAME_MARKET", selected_team=selected_team
        )
    if len(candidates) > 1 and len(uniq_ids) == 1:
        # Same id repeated — take first
        pass

    market = candidates[0]
    venue_name = (getattr(market, "venue", None) or venue or "polymarket").lower()

    if venue_name == "kalshi":
        outcome, side, rej, yes_team = _select_kalshi_outcome(
            market, selected_team, home_team, away_team
        )
        if rej:
            return MoneylineMatch(
                market=market,
                selected_team=selected_team,
                yes_team=yes_team,
                rejection_reason=rej,
            )
        return MoneylineMatch(
            market=market,
            outcome=outcome,
            selected_team=selected_team,
            side=side,
            yes_team=yes_team,
        )

    outcome, side, rej = _select_polymarket_outcome(
        market, selected_team, home_team, away_team
    )
    if rej:
        # If selected team is opponent of Yes-subject we returned No above;
        # TEAM_DIRECTION_MISMATCH means no team-named outcome matched.
        if rej == "TEAM_DIRECTION_MISMATCH":
            # Check whether Yes maps to the other team — if so and we wanted selected,
            # Polymarket Yes/No path should have returned No. Reach here for team-named miss.
            pass
        return MoneylineMatch(
            market=market, selected_team=selected_team, rejection_reason=rej
        )
    return MoneylineMatch(
        market=market,
        outcome=outcome,
        selected_team=selected_team,
        side=side,
        yes_team=resolve_kalshi_yes_team(market) if venue_name == "kalshi" else None,
    )
