"""
Prop consensus — aggregates O/U, BTTS, and draw picks into tradeable signals.

Stored in consensus_picks alongside moneyline rows, keyed by consensus_key.
"""
from __future__ import annotations

from collections import defaultdict

from loguru import logger

from backend.config import get_settings
from backend.db import get_db
from backend.ml.consensus_engine import (
    ELO_DEFAULT,
    MIN_RESOLVED_PICKS,
    ROOKIE_PENALTY,
    _clv_weight,
    _elo_weight,
)

PROP_BET_TYPES = ("total_goals", "btts", "draw")
MIN_PROP_PICKS = 2  # props need fewer pickers than moneyline


def _prop_min_picks() -> int:
    s = get_settings()
    if not s.polymarket_live_enabled:
        return s.consensus_min_picks_paper
    return max(MIN_PROP_PICKS, s.consensus_min_picks - 1)


def _consensus_key(bet_type: str, predicted_winner: str, bet_line: str | None) -> str:
    return f"{bet_type}|{predicted_winner}|{bet_line or ''}"


def _aggregate_prop_group(
    match_id: str,
    picks: list[dict],
    bet_type: str,
    predicted_winner: str,
    bet_line: str | None,
    *,
    min_picks: int,
) -> dict | None:
    if len(picks) < min_picks:
        return None

    db = get_db()
    influencer_ids = list({p["influencer_id"] for p in picks})
    influencers = (
        db.table("influencers")
        .select("id, elo_score, correct_picks, total_picks, avg_clv")
        .in_("id", influencer_ids)
        .execute()
        .data or []
    )
    inf_map = {
        inf["id"]: {
            "elo": inf.get("elo_score") or ELO_DEFAULT,
            "resolved": (inf.get("correct_picks") or 0) + max(
                0, (inf.get("total_picks") or 0) - (inf.get("correct_picks") or 0)
            ),
            "avg_clv": inf.get("avg_clv"),
        }
        for inf in influencers
    }

    total_weight = 0.0
    for pick in picks:
        conf = pick.get("confidence") or 0.55
        inf_data = inf_map.get(
            pick["influencer_id"],
            {"elo": ELO_DEFAULT, "resolved": 0, "avg_clv": None},
        )
        rookie_w = 1.0 if inf_data["resolved"] >= MIN_RESOLVED_PICKS else ROOKIE_PENALTY
        clv_w = _clv_weight(inf_data.get("avg_clv"), inf_data["resolved"])
        total_weight += _elo_weight(inf_data["elo"]) * conf * rookie_w * clv_w

    if total_weight <= 0:
        return None

    confidence = min(0.95, total_weight / (total_weight + 1.5))  # squash toward 0.5–0.9

    return {
        "match_id": match_id,
        "predicted_winner": predicted_winner,
        "bet_type": bet_type,
        "bet_line": bet_line,
        "consensus_key": _consensus_key(bet_type, predicted_winner, bet_line),
        "total_votes": len(picks),
        "weighted_score": round(total_weight, 4),
        "confidence": round(confidence, 4),
        "top_influencers": influencer_ids[:5],
        "pick_count": len(picks),
        "home_probability": 0.0,
        "draw_probability": 0.0,
        "away_probability": 0.0,
    }


def compute_prop_consensus_for_match(match_id: str) -> int:
    """Build consensus rows for props on one match. Returns count upserted."""
    db = get_db()
    min_picks = _prop_min_picks()

    picks = (
        db.table("picks")
        .select("predicted_winner, confidence, influencer_id, bet_type, bet_line")
        .eq("match_id", match_id)
        .eq("outcome", "pending")
        .in_("bet_type", list(PROP_BET_TYPES))
        .execute()
        .data or []
    )
    if not picks:
        return 0

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in picks:
        bt = p.get("bet_type") or "moneyline"
        pw = p.get("predicted_winner")
        bl = p.get("bet_line")
        if not pw:
            continue
        if bt == "total_goals":
            bl = bl or "2.5"
        groups[(bt, pw, bl if bt == "total_goals" else None)].append(p)

    upserted = 0
    for (bet_type, predicted_winner, bet_line), group in groups.items():
        record = _aggregate_prop_group(
            match_id, group, bet_type, predicted_winner, bet_line, min_picks=min_picks,
        )
        if not record:
            continue
        try:
            db.table("consensus_picks").upsert(
                record, on_conflict="match_id,consensus_key"
            ).execute()
            upserted += 1
        except Exception as exc:
            logger.debug(f"Prop consensus upsert fallback: {exc}")
            try:
                db.table("consensus_picks").upsert(
                    record, on_conflict="match_id,predicted_winner"
                ).execute()
                upserted += 1
            except Exception as exc2:
                logger.warning(f"Prop consensus failed for {match_id}: {exc2}")

    return upserted


def compute_all_prop_consensus() -> int:
    db = get_db()
    matches = (
        db.table("matches")
        .select("id")
        .eq("is_final", False)
        .execute()
        .data or []
    )
    total = 0
    for m in matches:
        total += compute_prop_consensus_for_match(m["id"])
    if total:
        logger.info(f"Computed {total} prop consensus signals")
    return total
