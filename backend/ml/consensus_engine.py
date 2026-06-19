"""
Consensus engine — aggregates all influencer picks for upcoming matches
and produces a weighted recommendation.

Weighting strategy:
  - Each pick gets a vote for a team
  - Vote is weighted by: influencer Elo score × pick confidence
  - Final confidence = weighted_vote_share for winning option
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from loguru import logger

from backend.db import get_db

ELO_DEFAULT = 1000.0
MIN_PICKS_FOR_CONSENSUS = 3  # need at least this many picks to make a recommendation


def compute_consensus_for_match(match_id: str) -> dict | None:
    """
    For a given match, aggregate all pending picks and produce a consensus.
    Returns the consensus record, or None if insufficient data.
    """
    db = get_db()

    # Fetch all picks for this match with influencer Elo
    picks = (
        db.table("picks")
        .select("predicted_winner, confidence, influencer_id")
        .eq("match_id", match_id)
        .eq("outcome", "pending")
        .execute()
        .data or []
    )

    if len(picks) < MIN_PICKS_FOR_CONSENSUS:
        return None

    # Fetch influencer Elo scores in one query
    influencer_ids = list({p["influencer_id"] for p in picks})
    influencers = (
        db.table("influencers")
        .select("id, elo_score")
        .in_("id", influencer_ids)
        .execute()
        .data or []
    )
    elo_map = {inf["id"]: inf.get("elo_score") or ELO_DEFAULT for inf in influencers}

    # Accumulate weighted votes
    vote_weights: dict[str, float] = defaultdict(float)
    vote_counts: dict[str, int] = defaultdict(int)
    top_supporters: dict[str, list[tuple[float, str]]] = defaultdict(list)

    for pick in picks:
        team = pick.get("predicted_winner")
        if not team:
            continue
        conf = pick.get("confidence") or 0.55
        elo = elo_map.get(pick["influencer_id"], ELO_DEFAULT)
        # Normalise Elo so average influencer has weight 1.0
        elo_weight = elo / ELO_DEFAULT
        weight = elo_weight * conf
        vote_weights[team] += weight
        vote_counts[team] += 1
        top_supporters[team].append((weight, pick["influencer_id"]))

    if not vote_weights:
        return None

    total_weight = sum(vote_weights.values())
    best_team = max(vote_weights, key=lambda t: vote_weights[t])
    weighted_score = vote_weights[best_team]
    confidence = weighted_score / total_weight if total_weight > 0 else 0.0

    # Top 5 influencers backing the best team
    supporters = sorted(top_supporters[best_team], key=lambda x: x[0], reverse=True)
    top_5_ids = [s[1] for s in supporters[:5]]

    record = {
        "match_id": match_id,
        "predicted_winner": best_team,
        "total_votes": vote_counts[best_team],
        "weighted_score": round(weighted_score, 4),
        "confidence": round(confidence, 4),
        "top_influencers": top_5_ids,
    }

    db.table("consensus_picks").upsert(
        record, on_conflict="match_id,predicted_winner"
    ).execute()

    return record


def compute_all_consensus() -> int:
    """Compute consensus for all upcoming (non-finished) matches."""
    db = get_db()
    matches = (
        db.table("matches")
        .select("id")
        .eq("is_final", False)
        .execute()
        .data or []
    )
    computed = 0
    for match in matches:
        result = compute_consensus_for_match(match["id"])
        if result:
            computed += 1
    logger.info(f"Computed consensus for {computed}/{len(matches)} upcoming matches")
    return computed


def get_top_recommendations(limit: int = 10) -> list[dict]:
    """
    Return the top N recommended picks across all upcoming matches,
    sorted by consensus confidence.
    """
    db = get_db()
    rows = (
        db.table("consensus_picks")
        .select("*, matches(home_team, away_team, scheduled_at, stage)")
        .order("confidence", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )
    return rows
