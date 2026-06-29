"""
Simulated (paper) betting engine.

Mirrors the consensus picks into a virtual bankroll using Kelly Criterion
sizing.  All bets are virtual — no real money, no real bookmaker integration.

Purpose:
  1. Validate whether the consensus model has positive expected value before
     committing to live bets.
  2. Give the dashboard a "track record" with dollar figures people understand.
  3. Generate a natural edge score per match.

Kelly formula (fractional):
  f* = (b·p - q) / b
  where:
    p = estimated win probability (consensus confidence)
    q = 1 - p
    b = decimal odds - 1  (profit per unit staked)
  We use a half-Kelly (f*/2) by default for conservatism.

Implied decimal odds are derived from confidence: odds = 1/confidence.
In practice you'd replace this with real bookmaker lines; for now we use
the consensus as both the probability estimate and the "market line."
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from backend.db import get_db

STARTING_BANKROLL = 1000.0   # virtual dollars
HALF_KELLY = True            # use half-Kelly for conservatism
MIN_EDGE = 0.03              # skip bets with < 3% edge
MAX_BET_FRACTION = 0.15      # never bet more than 15% of bankroll on one match


def kelly_fraction(p: float, decimal_odds: float) -> float:
    """
    Full Kelly fraction for a binary outcome.
    p            — estimated win probability
    decimal_odds — e.g. 2.5 means you get $2.50 back per $1 staked
    """
    b = decimal_odds - 1  # net profit per unit
    q = 1 - p
    if b <= 0:
        return 0.0
    f = (b * p - q) / b
    if HALF_KELLY:
        f = f / 2
    return max(0.0, min(f, MAX_BET_FRACTION))


def implied_odds(confidence: float) -> float:
    """Fair decimal odds derived from consensus confidence."""
    if confidence <= 0 or confidence >= 1:
        return 1.0
    return 1.0 / confidence


def _current_bankroll(db) -> float:
    """Get current virtual bankroll from last resolved bet, or STARTING_BANKROLL."""
    last = (
        db.table("simulated_bets")
        .select("bankroll_at_time, pnl")
        .not_.is_("resolved_at", "null")
        .order("resolved_at", desc=True)
        .limit(1)
        .execute()
        .data or []
    )
    if not last:
        return STARTING_BANKROLL
    row = last[0]
    return (row.get("bankroll_at_time") or STARTING_BANKROLL) + (row.get("pnl") or 0.0)


def place_paper_bets() -> int:
    """
    For every high-confidence upcoming consensus pick, create a simulated_bets
    row if one doesn't already exist for that match + bet type.
    Returns the number of new bets placed.
    """
    from backend.config import get_settings

    db = get_db()
    s = get_settings()
    bankroll = _current_bankroll(db)
    min_picks_paper = s.consensus_min_picks_paper

    rows = (
        db.table("consensus_picks")
        .select(
            "match_id, predicted_winner, confidence, pick_count, bet_type, bet_line, "
            "matches(home_team, away_team, scheduled_at, is_final, sport)"
        )
        .execute()
        .data or []
    )

    placed = 0
    for row in rows:
        match = row.get("matches") or {}
        if match.get("is_final"):
            continue
        pick_count = row.get("pick_count") or 0
        min_picks = min_picks_paper if not s.polymarket_live_enabled else 3
        if pick_count < min_picks:
            continue

        confidence = row.get("confidence") or 0.0
        bet_type = row.get("bet_type") or "moneyline"
        min_conf = (
            s.polymarket_paper_min_prop_confidence
            if bet_type in ("total_goals", "btts", "draw")
            else s.polymarket_paper_min_consensus_confidence
        )
        if not s.polymarket_live_enabled and confidence < min_conf:
            continue

        odds = implied_odds(confidence)
        b = odds - 1
        q = 1 - confidence
        edge = b * confidence - q

        if edge < MIN_EDGE and s.polymarket_live_enabled:
            continue
        if edge < 0.01 and not s.polymarket_live_enabled:
            continue  # paper: allow small positive edge for track record

        bet_line = row.get("bet_line")
        predicted = row["predicted_winner"]
        dup = (
            db.table("simulated_bets")
            .select("id")
            .eq("match_id", row["match_id"])
            .eq("bet_type", bet_type)
            .eq("predicted_outcome", predicted)
            .is_("resolved_at", "null")
        )
        if bet_line:
            dup = dup.eq("bet_line", bet_line)
        if dup.execute().data:
            continue

        f = kelly_fraction(confidence, odds)
        bet_size = round(bankroll * f, 2)
        if bet_size < 1.0:
            continue

        db.table("simulated_bets").insert({
            "match_id": row["match_id"],
            "predicted_outcome": predicted,
            "bet_type": bet_type,
            "bet_line": bet_line,
            "confidence": confidence,
            "edge": round(edge, 4),
            "kelly_fraction": round(f, 4),
            "bet_size": bet_size,
            "bankroll_at_time": round(bankroll, 2),
        }).execute()

        bankroll -= bet_size
        placed += 1

    logger.info(f"Paper trading: {placed} new simulated bets placed")
    return placed


def resolve_paper_bets() -> int:
    """
    For each unresolved simulated bet whose match is now finished,
    calculate P&L and mark as resolved.
    Returns the number of bets resolved.
    """
    from backend.sports_data.bet_settlement import grade_pick

    db = get_db()

    unresolved = (
        db.table("simulated_bets")
        .select(
            "id, match_id, predicted_outcome, bet_type, bet_line, bet_subject, "
            "confidence, bet_size"
        )
        .is_("resolved_at", "null")
        .execute()
        .data or []
    )
    if not unresolved:
        return 0

    match_ids = [b["match_id"] for b in unresolved]
    finished = (
        db.table("matches")
        .select(
            "id, winner, home_score, away_score, is_final, home_team, away_team, match_stats"
        )
        .in_("id", match_ids)
        .eq("is_final", True)
        .execute()
        .data or []
    )
    finished_map = {m["id"]: m for m in finished}

    resolved = 0
    for bet in unresolved:
        match = finished_map.get(bet["match_id"])
        if not match:
            continue

        bet_type = bet.get("bet_type") or "moneyline"
        grade = grade_pick(
            bet_type=bet_type,
            predicted_winner=bet["predicted_outcome"],
            bet_line=bet.get("bet_line"),
            bet_subject=bet.get("bet_subject"),
            match=match,
            match_stats=match.get("match_stats"),
        )
        if grade is None:
            continue
        if grade == "void":
            db.table("simulated_bets").update({
                "outcome": "void",
                "pnl": 0.0,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", bet["id"]).execute()
            resolved += 1
            continue

        is_correct = grade == "correct"
        confidence = bet.get("confidence") or 0.5
        odds = implied_odds(confidence)
        bet_size = bet.get("bet_size") or 0.0

        pnl = round((odds - 1) * bet_size if is_correct else -bet_size, 2)
        outcome = "correct" if is_correct else "incorrect"

        db.table("simulated_bets").update({
            "outcome": outcome,
            "pnl": pnl,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", bet["id"]).execute()
        resolved += 1

    if resolved:
        logger.info(f"Paper trading: {resolved} bets resolved")
    return resolved


def get_paper_trading_summary() -> dict[str, Any]:
    """Return a summary of paper trading performance."""
    db = get_db()
    all_bets = (
        db.table("simulated_bets")
        .select("bet_size, pnl, outcome, confidence")
        .execute()
        .data or []
    )
    if not all_bets:
        return {
            "bankroll": STARTING_BANKROLL,
            "starting_bankroll": STARTING_BANKROLL,
            "total_pnl": 0.0,
            "total_bets": 0,
            "pending_bets": 0,
            "win_rate": 0.0,
            "roi_pct": 0.0,
            "total_wagered": 0.0,
        }

    resolved = [b for b in all_bets if b.get("outcome")]
    total_wagered = sum(b.get("bet_size") or 0 for b in resolved)
    total_pnl = sum(b.get("pnl") or 0 for b in resolved)
    wins = sum(1 for b in resolved if b.get("outcome") == "correct")
    current_bankroll = STARTING_BANKROLL + total_pnl

    return {
        "bankroll": round(current_bankroll, 2),
        "starting_bankroll": STARTING_BANKROLL,
        "total_pnl": round(total_pnl, 2),
        "total_bets": len(resolved),
        "pending_bets": len(all_bets) - len(resolved),
        "win_rate": round(wins / len(resolved), 4) if resolved else 0.0,
        "roi_pct": round(total_pnl / total_wagered * 100, 2) if total_wagered else 0.0,
        "total_wagered": round(total_wagered, 2),
    }
