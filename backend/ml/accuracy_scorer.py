"""
Accuracy scoring utilities.

Computes per-influencer and overall platform statistics.
Also ranks influencers for the leaderboard.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from backend.db import get_db


def get_leaderboard(
    limit: int = 50,
    min_picks: int = 3,
    sort_by: str = "elo_score",  # or 'accuracy_rate'
) -> list[dict]:
    """Return top influencers ranked by Elo score (default) or accuracy."""
    db = get_db()
    rows = (
        db.table("influencers")
        .select(
            "id, platform, handle, display_name, profile_url, avatar_url, "
            "follower_count, elo_score, accuracy_rate, total_picks, correct_picks, "
            "pick_streak, consensus_score, wilson_score, last_scraped_at"
        )
        .eq("is_active", True)
        .gte("total_picks", min_picks)
        .order(sort_by, desc=True)
        .limit(limit)
        .execute()
        .data or []
    )
    for i, row in enumerate(rows):
        row["rank"] = i + 1
    return rows


def compute_pick_streaks() -> int:
    """Update pick_streak for every influencer (+ for wins, – for losses)."""
    db = get_db()
    influencers = (
        db.table("influencers")
        .select("id")
        .eq("is_active", True)
        .execute()
        .data or []
    )
    updated = 0
    for inf in influencers:
        iid = inf["id"]
        recent = (
            db.table("picks")
            .select("outcome")
            .eq("influencer_id", iid)
            .in_("outcome", ["correct", "incorrect"])
            .order("resolved_at", desc=True)
            .limit(20)
            .execute()
            .data or []
        )
        if not recent:
            continue
        streak = 0
        first_outcome = recent[0]["outcome"]
        for p in recent:
            if p["outcome"] == first_outcome:
                streak += 1 if first_outcome == "correct" else -1
            else:
                break
        db.table("influencers").update({"pick_streak": streak}).eq("id", iid).execute()
        updated += 1
    logger.info(f"Updated streaks for {updated} influencers")
    return updated


def compute_consensus_scores() -> int:
    """
    For each influencer, measure how often their picks agree with the
    consensus_picks winner. Stored as consensus_score (0.0 – 1.0).
    """
    db = get_db()
    # Get all resolved consensus picks
    consensus = (
        db.table("consensus_picks")
        .select("match_id, predicted_winner")
        .execute()
        .data or []
    )
    consensus_map = {c["match_id"]: c["predicted_winner"] for c in consensus}
    if not consensus_map:
        return 0

    influencers = (
        db.table("influencers")
        .select("id")
        .eq("is_active", True)
        .execute()
        .data or []
    )
    updated = 0
    for inf in influencers:
        iid = inf["id"]
        picks = (
            db.table("picks")
            .select("match_id, predicted_winner")
            .eq("influencer_id", iid)
            .not_.is_("match_id", "null")
            .execute()
            .data or []
        )
        if not picks:
            continue
        agree = sum(
            1 for p in picks
            if p["match_id"] in consensus_map
            and p["predicted_winner"] == consensus_map[p["match_id"]]
        )
        total = len([p for p in picks if p["match_id"] in consensus_map])
        if total == 0:
            continue
        score = round(agree / total, 4)
        db.table("influencers").update({"consensus_score": score}).eq("id", iid).execute()
        updated += 1

    logger.info(f"Updated consensus scores for {updated} influencers")
    return updated
