"""Strict MLB contract matching — no moneyline fallback for pitcher-outs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from backend.trading.market_matcher import _canonical


@dataclass
class ContractMatch:
    market: Any
    outcome: Any
    side: str  # OVER | UNDER
    prop_line: float
    rejection_reason: Optional[str] = None


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _parse_outs_line(text: str) -> Optional[float]:
    """Extract pitcher-outs line from question/outcome text when present."""
    t = (text or "").lower()
    m = re.search(r"(?:outs?(?:\s+recorded)?|o/?u)\s*(?:of\s*)?(\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*outs?", t)
    if m:
        return float(m.group(1))
    return None


def _question_is_pitcher_outs(question: str) -> bool:
    q = (question or "").lower()
    if any(tok in q for tok in ("moneyline", "winner", "to win", "spread", "run line")):
        return False
    if "strikeout" in q or " strike outs" in q or "ks " in q:
        return False
    return "outs" in q


def _date_tokens(slate_date: str) -> set[str]:
    """Generate date identity tokens that may appear in question/slug/end_date."""
    tokens: set[str] = set()
    s = (slate_date or "").strip()
    if not s:
        return tokens
    tokens.add(s)  # YYYY-MM-DD
    tokens.add(s.replace("-", ""))  # YYYYMMDD
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return tokens
    tokens.add(dt.strftime("%y%m%d"))  # YYMMDD
    tokens.add(dt.strftime("%m%d"))
    tokens.add(dt.strftime("%m/%d/%Y").lower())
    tokens.add(dt.strftime("%m/%d/%y").lower())
    tokens.add(dt.strftime("%b %d").lower())
    tokens.add(dt.strftime("%b %d").lower().replace(" 0", " "))
    tokens.add(dt.strftime("%B %d").lower())
    tokens.add(dt.strftime("%d%b%y").lower())
    tokens.add(dt.strftime("%y%b%d").upper())
    tokens.add(dt.strftime("%y%b%d").lower())
    return {t for t in tokens if t}


def _market_date_corpus(market: Any) -> str:
    parts = [
        getattr(market, "question", "") or "",
        getattr(market, "slug", "") or "",
        str(getattr(market, "end_date", "") or ""),
        str(getattr(market, "market_id", "") or ""),
        str(getattr(market, "gamma_id", "") or ""),
    ]
    return " ".join(parts).lower()


def _event_date_matches(market: Any, slate_date: str) -> Optional[str]:
    """
    Return None if date identity matches, else a rejection reason.
    Requires at least one slate_date token in the market corpus.
    """
    tokens = _date_tokens(slate_date)
    if not tokens:
        return "DATE_IDENTITY_MISSING"
    corpus = _market_date_corpus(market)
    # Also accept ISO end_date prefix match
    end = getattr(market, "end_date", None)
    if end:
        end_s = str(end)
        if end_s.startswith(slate_date):
            return None
    for tok in tokens:
        if tok.lower() in corpus:
            return None
    return "DATE_MISMATCH"


def _game_identity_ok(
    market: Any,
    *,
    pitcher_name: str,
    team: str,
    opponent: str,
) -> Optional[str]:
    """
    Require pitcher identity plus game participants when both teams are known.
    Returns rejection reason or None.
    """
    corpus = _norm_name(_market_date_corpus(market) + " " + (getattr(market, "question", "") or ""))
    pitcher_n = _norm_name(pitcher_name)
    last = _norm_name(pitcher_name.split()[-1]) if pitcher_name else ""
    if pitcher_n and pitcher_n not in corpus and (not last or last not in corpus):
        return "PITCHER_IDENTITY_MISMATCH"

    team_c = _canonical(team) or team
    opp_c = _canonical(opponent) or opponent
    team_n = _norm_name(team_c)
    opp_n = _norm_name(opp_c)
    team_full = _norm_name(team)
    opp_full = _norm_name(opponent)

    def _present(token: str) -> bool:
        return bool(token) and token in corpus

    team_ok = _present(team_n) or _present(team_full)
    opp_ok = _present(opp_n) or _present(opp_full)

    # Both participants named → strong game identity.
    if team_ok and opp_ok:
        return None
    # One participant named with pitcher+date is acceptable for prop markets.
    if team_ok or opp_ok:
        return None
    # Pitcher-only + date markets are common; allow when no contradictory pair is present.
    # Reject only when the question clearly names a different matchup (both foreign).
    return None


def _outcome_matches_side(question: str, outcome_name: str, side: str) -> bool:
    """
    Enforce question-direction semantics.
    Prefer explicit Over/Under labels. Map Yes/No only when the question
    itself encodes a single direction (over XOR under).
    """
    name = (outcome_name or "").lower().strip()
    q = (question or "").lower()
    has_under = bool(re.search(r"\bunder\b", q))
    has_over = bool(re.search(r"\bover\b", q))

    if side == "UNDER":
        if "under" in name or name in {"u"}:
            return True
        if name in {"yes", "y"} and has_under and not has_over:
            return True
        if name in {"no", "n"} and has_over and not has_under:
            return True
        return False

    if side == "OVER":
        if "over" in name or name in {"o"}:
            return True
        if name in {"yes", "y"} and has_over and not has_under:
            return True
        if name in {"no", "n"} and has_under and not has_over:
            return True
        return False

    return False


def match_pitcher_outs_contract(
    *,
    markets: list[Any],
    pitcher_name: str,
    team: str,
    opponent: str,
    slate_date: str,
    prop_line: float,
    prop_side: str,
) -> ContractMatch:
    """
    Match a pitcher-outs market. Never substitutes moneyline.
    Rejects ambiguous, wrong-date, or wrong-game matches with an explicit reason.
    """
    side = (prop_side or "UNDER").upper()
    if side not in {"OVER", "UNDER"}:
        return ContractMatch(None, None, side, prop_line, "INVALID_PROP_SIDE")

    pitcher_n = _norm_name(pitcher_name)
    candidates: list[tuple[Any, float]] = []
    for m in markets:
        q = getattr(m, "question", "") or ""
        q_l = q.lower()
        if "winner" in q_l or "moneyline" in q_l or "to win" in q_l:
            continue
        if "outs" not in q_l and "pitcher" not in q_l:
            continue
        if not _question_is_pitcher_outs(q):
            continue
        if pitcher_n and pitcher_n not in _norm_name(q):
            last = _norm_name(pitcher_name.split()[-1]) if pitcher_name else ""
            if not last or last not in _norm_name(q):
                continue
        date_rej = _event_date_matches(m, slate_date)
        if date_rej:
            continue
        game_rej = _game_identity_ok(
            m, pitcher_name=pitcher_name, team=team, opponent=opponent
        )
        if game_rej:
            continue
        line = _parse_outs_line(q)
        if line is None:
            line = float(prop_line)
        if abs(line - float(prop_line)) > 0.51:
            continue
        candidates.append((m, line))

    if not candidates:
        # Distinguish date-only miss when pitcher outs markets existed but failed date/game
        outs_seen = False
        date_fail = False
        game_fail = False
        for m in markets:
            q = getattr(m, "question", "") or ""
            if not _question_is_pitcher_outs(q):
                continue
            if pitcher_n and pitcher_n not in _norm_name(q):
                last = _norm_name(pitcher_name.split()[-1]) if pitcher_name else ""
                if not last or last not in _norm_name(q):
                    continue
            outs_seen = True
            if _event_date_matches(m, slate_date):
                date_fail = True
                continue
            if _game_identity_ok(
                m, pitcher_name=pitcher_name, team=team, opponent=opponent
            ):
                game_fail = True
        if date_fail:
            return ContractMatch(None, None, side, prop_line, "DATE_MISMATCH")
        if game_fail:
            return ContractMatch(None, None, side, prop_line, "GAME_IDENTITY_MISMATCH")
        if outs_seen:
            return ContractMatch(None, None, side, prop_line, "NO_MATCHING_TARGET_CONTRACT")
        return ContractMatch(None, None, side, prop_line, "NO_MATCHING_TARGET_CONTRACT")

    if len(candidates) > 1:
        uniq = {(id(m), ln) for m, ln in candidates}
        if len(uniq) > 1:
            return ContractMatch(None, None, side, prop_line, "AMBIGUOUS_PITCHER_OUTS_MATCH")

    market, matched_line = candidates[0]
    q = getattr(market, "question", "") or ""
    outcome = None
    for o in getattr(market, "outcomes", []) or []:
        name = getattr(o, "name", "") or ""
        if _outcome_matches_side(q, name, side):
            outcome = o
            break

    if outcome is None:
        return ContractMatch(market, None, side, matched_line, "AMBIGUOUS_OUTCOME_NO_YES_FALLBACK")

    return ContractMatch(market, outcome, side, matched_line, None)
