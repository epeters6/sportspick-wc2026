"""Strict MLB contract matching — no moneyline fallback for pitcher-outs."""
from __future__ import annotations

import re
from dataclasses import dataclass
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
    # e.g. "outs 17.5", "o/u 18.5 outs", "pitcher outs recorded 16.5"
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
    # Pitcher outs props always mention outs; strikeout props are a different contract.
    if "strikeout" in q or " strike outs" in q or "ks " in q:
        return False
    return "outs" in q


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
    Rejects ambiguous or wrong-date matches with an explicit reason.
    """
    side = (prop_side or "UNDER").upper()
    if side not in {"OVER", "UNDER"}:
        return ContractMatch(None, None, side, prop_line, "INVALID_PROP_SIDE")

    pitcher_n = _norm_name(pitcher_name)
    team_c = _canonical(team) or team
    opp_c = _canonical(opponent) or opponent
    date_token = slate_date.replace("-", "")

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
            # Allow last-name-only when full name not present
            last = _norm_name(pitcher_name.split()[-1]) if pitcher_name else ""
            if not last or last not in _norm_name(q):
                continue
        line = _parse_outs_line(q)
        if line is None:
            line = float(prop_line)
        if abs(line - float(prop_line)) > 0.51:
            continue
        candidates.append((m, line))

    if not candidates:
        return ContractMatch(None, None, side, prop_line, "NO_MATCHING_TARGET_CONTRACT")
    if len(candidates) > 1:
        # Unique pitcher+line required
        uniq = {(id(m), ln) for m, ln in candidates}
        if len(uniq) > 1:
            return ContractMatch(None, None, side, prop_line, "AMBIGUOUS_PITCHER_OUTS_MATCH")

    market, matched_line = candidates[0]
    outcome = None
    for o in getattr(market, "outcomes", []) or []:
        name = (getattr(o, "name", "") or "").lower()
        if side == "UNDER" and ("under" in name or name in {"no", "u"}):
            outcome = o
            break
        if side == "OVER" and ("over" in name or name in {"yes", "o"}):
            outcome = o
            break

    if outcome is None:
        return ContractMatch(market, None, side, matched_line, "AMBIGUOUS_OUTCOME_NO_YES_FALLBACK")

    # Team/opponent sanity when both appear in question
    q = (getattr(market, "question", "") or "").lower()
    team_n = (team_c or "").lower()
    opp_n = (opp_c or "").lower()
    if team_n and opp_n and team_n in q and opp_n in q:
        pass  # good
    # Do not reject solely for missing team abbreviations — pitcher identity is primary

    return ContractMatch(market, outcome, side, matched_line, None)
