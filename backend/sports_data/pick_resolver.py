"""Resolve pending picks for all sports using match_stats."""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from backend.db import get_db
from backend.sports_data.bet_settlement import grade_pick
from backend.sports_data.mlb_player_linking import infer_mlb_match_for_player_pick
from backend.sports_data.pick_linking import (
    build_match_index,
    infer_match_candidates,
    normalize_player_subject,
    pick_best_match,
)


def _resolve_unlinked_player_picks(db) -> int:
    """Grade player props that couldn't persist match_id (duplicate influencer/match)."""
    unlinked = (
        db.table("picks")
        .select(
            "id, predicted_winner, posted_at, bet_type, bet_line, bet_subject, raw_text"
        )
        .eq("outcome", "pending")
        .like("bet_type", "player_%")
        .is_("match_id", "null")
        .execute()
        .data or []
    )
    if not unlinked:
        return 0

    matches = (
        db.table("matches")
        .select(
            "id, sport, home_team, away_team, winner, home_score, away_score, "
            "is_final, match_stats, scheduled_at"
        )
        .execute()
        .data or []
    )
    if not matches:
        return 0

    mlb_matches = [m for m in matches if m.get("sport") == "mlb"]
    by_team, alias_to_canonical = build_match_index(matches)
    match_map = {m["id"]: m for m in matches}
    resolved = 0

    for pick in unlinked:
        candidates = infer_match_candidates(pick, matches, by_team, alias_to_canonical)
        best = pick_best_match(candidates, pick.get("posted_at"), pick.get("raw_text"))
        if not best and mlb_matches:
            best = infer_mlb_match_for_player_pick(pick, mlb_matches)
        if not best:
            continue
        match = match_map.get(best["id"])
        if not match or not match.get("is_final"):
            continue
        grade = grade_pick(
            bet_type=pick.get("bet_type") or "moneyline",
            predicted_winner=pick.get("predicted_winner") or "",
            bet_line=pick.get("bet_line"),
            bet_subject=normalize_player_subject(pick.get("bet_subject")),
            match=match,
            match_stats=match.get("match_stats"),
        )
        if grade is None:
            continue
        db.table("picks").update({
            "outcome": grade,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", pick["id"]).execute()
        resolved += 1

    if resolved:
        logger.info(f"Resolved {resolved} unlinked player props")
    return resolved


def resolve_all_pending_picks() -> int:
    db = get_db()
    pending = (
        db.table("picks")
        .select(
            "id, match_id, predicted_winner, bet_type, bet_line, bet_subject"
        )
        .eq("outcome", "pending")
        .not_.is_("match_id", "null")
        .execute()
        .data or []
    )
    if not pending:
        return _resolve_unlinked_player_picks(db)

    match_ids = list({p["match_id"] for p in pending if p.get("match_id")})
    matches = (
        db.table("matches")
        .select(
            "id, sport, home_team, away_team, winner, home_score, away_score, "
            "is_final, match_stats"
        )
        .in_("id", match_ids)
        .execute()
        .data or []
    )
    match_map = {m["id"]: m for m in matches}

    resolved = 0
    voided = 0
    for pick in pending:
        match = match_map.get(pick.get("match_id"))
        if not match or not match.get("is_final"):
            continue
        grade = grade_pick(
            bet_type=pick.get("bet_type") or "moneyline",
            predicted_winner=pick.get("predicted_winner") or "",
            bet_line=pick.get("bet_line"),
            bet_subject=pick.get("bet_subject"),
            match=match,
            match_stats=match.get("match_stats"),
        )
        if grade is None:
            continue
        db.table("picks").update({
            "outcome": grade,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", pick["id"]).execute()
        resolved += 1
        if grade == "void":
            voided += 1

    if resolved:
        logger.info(f"Resolved {resolved} picks ({voided} void)")

    resolved += _resolve_unlinked_player_picks(db)
    return resolved
