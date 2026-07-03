"""
Elo ranking engine for influencer accuracy.

Each influencer starts at Elo 1000. When a pick resolves:
- Correct pick   → Elo increases (more if they were the underdog opinion)
- Incorrect pick → Elo decreases

Improvements over vanilla Elo:
- Recency half-life weighting (recent picks matter more)
- Confidence-scaled K-factor (high-confidence wrong pick hurts more)
- Wilson lower-bound score: a sample-size-aware "true accuracy" estimate
  that penalises small-sample flukes, used as a trust multiplier in consensus.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from loguru import logger

from backend.db import get_db

ELO_K = 32          # standard K-factor
ELO_DEFAULT = 1000  # starting Elo
RECENCY_HALFLIFE_DAYS = 30  # picks older than this decay in weight
WILSON_Z = 1.645    # 95% confidence interval (one-sided)


def expected_score(elo_a: float, elo_b: float) -> float:
    """Expected score for player A vs. player B."""
    return 1.0 / (1.0 + math.pow(10, (elo_b - elo_a) / 400))


def update_elo(current_elo: float, actual_score: float, expected: float, k: float = ELO_K) -> float:
    return current_elo + k * (actual_score - expected)


def wilson_lower_bound(correct: int, total: int, z: float = WILSON_Z) -> float:
    """
    Wilson score interval lower bound — a sample-size-aware accuracy estimate.

    For n=0  → 0.0 (no data, no trust)
    For n=3, 3/3 → ~0.49 (good but small sample)
    For n=20, 15/20 → ~0.57 (solid)
    For n=50, 40/50 → ~0.68 (strong)

    This prevents a 2-for-2 picker from outranking a 40-for-50 picker.
    """
    if total == 0:
        return 0.0
    p_hat = correct / total
    denominator = 1 + z * z / total
    centre = p_hat + z * z / (2 * total)
    spread = z * math.sqrt(p_hat * (1 - p_hat) / total + z * z / (4 * total * total))
    return (centre - spread) / denominator


def recency_weight(posted_at: str | None) -> float:
    """Returns a weight in (0, 1] based on how recent the pick is."""
    if not posted_at:
        return 1.0
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - dt).days
        return math.exp(-age_days * math.log(2) / RECENCY_HALFLIFE_DAYS)
    except Exception:
        return 1.0


def confidence_k_scale(confidence: float | None) -> float:
    """
    Scale the K-factor by pick confidence.
    High-confidence picks move the needle more (both up and down).
    Range: 0.7 × K (low conf) to 1.3 × K (high conf).
    """
    if confidence is None:
        return 1.0
    # Map [0, 1] → [0.7, 1.3]
    return 0.7 + 0.6 * confidence


# ─── Main update function ────────────────────────────────────────────────────

def update_all_elo_scores() -> int:
    """
    Recalculate Elo for every influencer based on all resolved picks.
    Stores per-sport Elo in elo_by_sport (football vs mlb) so WC volume
    does not dominate MLB influencer rankings.
    """
    db = get_db()

    picks = (
        db.table("picks")
        .select(
            "influencer_id, outcome, posted_at, confidence, market_prob_at_pick, "
            "match_id, matches(sport)"
        )
        .in_("outcome", ["correct", "incorrect"])
        .order("posted_at")
        .execute()
        .data or []
    )

    if not picks:
        return 0

    def _sport_key(pick: dict) -> str:
        sport = (pick.get("matches") or {}).get("sport") or "football"
        return str(sport).lower()

    influencer_picks: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        influencer_picks[p["influencer_id"]].append(p)

    def _compute_elo(ipicks: list[dict]) -> tuple[float, int, int, float]:
        elo = float(ELO_DEFAULT)
        total = 0
        correct = 0
        for pick in sorted(ipicks, key=lambda p: p.get("posted_at") or ""):
            recency_w = recency_weight(pick.get("posted_at"))
            conf_scale = confidence_k_scale(pick.get("confidence"))
            actual = 1.0 if pick["outcome"] == "correct" else 0.0
            market_prob = pick.get("market_prob_at_pick")
            if market_prob is not None and 0.0 < market_prob < 1.0:
                expected = market_prob
            else:
                expected = expected_score(elo, ELO_DEFAULT)
            k_adj = ELO_K * recency_w * conf_scale
            elo = update_elo(elo, actual, expected, k_adj)
            total += 1
            if pick["outcome"] == "correct":
                correct += 1
        accuracy = correct / total if total else 0.0
        wlb = wilson_lower_bound(correct, total)
        return round(elo, 2), total, correct, round(wlb, 4)

    for iid, ipicks in influencer_picks.items():
        by_sport: dict[str, list[dict]] = defaultdict(list)
        for pick in ipicks:
            by_sport[_sport_key(pick)].append(pick)

        elo_by_sport: dict[str, float] = {}

        for sport, spicks in by_sport.items():
            if not spicks:
                continue
            elo, _, _, _ = _compute_elo(spicks)
            elo_by_sport[sport] = elo

        primary = max(by_sport.keys(), key=lambda s: len(by_sport[s]))
        headline_elo, headline_total, headline_correct, headline_wlb = _compute_elo(
            by_sport[primary]
        )

        db.table("influencers").update(
            {
                "elo_score": headline_elo,
                "elo_by_sport": elo_by_sport,
                "accuracy_rate": round(
                    headline_correct / headline_total if headline_total else 0.0, 4
                ),
                "total_picks": headline_total,
                "correct_picks": headline_correct,
                "wilson_score": headline_wlb,
            }
        ).eq("id", iid).execute()

    logger.info(f"Elo updated for {len(influencer_picks)} influencers (per-sport)")
    return len(influencer_picks)


def sync_influencer_pick_counts() -> int:
    """
    Update total_picks on every influencer to reflect the actual count of all
    picks (including pending ones). Runs fast — one query, one batch update.
    Returns the number of influencers updated.
    """
    db = get_db()

    all_picks = (
        db.table("picks")
        .select("influencer_id")
        .execute()
        .data or []
    )

    from collections import Counter
    counts: Counter = Counter(p["influencer_id"] for p in all_picks)

    updated = 0
    for influencer_id, count in counts.items():
        try:
            db.table("influencers").update(
                {"total_picks": count}
            ).eq("id", influencer_id).execute()
            updated += 1
        except Exception as exc:
            logger.warning(f"Failed to sync pick count for {influencer_id}: {exc}")

    logger.info(f"Synced pick counts for {updated} influencers")
    return updated


def deactivate_poor_performers(
    min_picks: int = 5,
    elo_cutoff: float = 950.0,
) -> int:
    """
    Mark influencers as inactive if they've made enough picks to be judged
    but have fallen well below the starting Elo of 1000.

    Elo < 950 after 5+ picks means they've been more wrong than right
    — not worth continuing to track them.
    Covers.com experts are never deactivated (they're vetted professionals).
    """
    db = get_db()
    result = (
        db.table("influencers")
        .update({"is_active": False})
        .gte("total_picks", min_picks)
        .lt("elo_score", elo_cutoff)
        .neq("platform", "covers")
        .execute()
    )
    count = len(result.data or [])
    if count:
        logger.info(
            f"Deactivated {count} poor-performing influencer(s) "
            f"(≥{min_picks} picks, Elo < {elo_cutoff})"
        )
    return count


def snapshot_daily_stats() -> int:
    """Save a daily snapshot of all influencer stats for trend tracking."""
    db = get_db()
    influencers = (
        db.table("influencers")
        .select("id, elo_score, accuracy_rate, total_picks, correct_picks")
        .execute()
        .data or []
    )

    # Compute ranks
    sorted_by_elo = sorted(influencers, key=lambda x: x.get("elo_score") or 0, reverse=True)
    sorted_by_acc = sorted(influencers, key=lambda x: x.get("accuracy_rate") or 0, reverse=True)
    elo_rank = {inf["id"]: i + 1 for i, inf in enumerate(sorted_by_elo)}
    acc_rank = {inf["id"]: i + 1 for i, inf in enumerate(sorted_by_acc)}

    today = datetime.now(timezone.utc).date().isoformat()
    records = []
    for inf in influencers:
        records.append({
            "influencer_id": inf["id"],
            "snapshot_date": today,
            "elo_score": inf.get("elo_score"),
            "accuracy_rate": inf.get("accuracy_rate"),
            "total_picks": inf.get("total_picks"),
            "correct_picks": inf.get("correct_picks"),
            "elo_rank": elo_rank.get(inf["id"]),
            "accuracy_rank": acc_rank.get(inf["id"]),
        })

    if records:
        db.table("influencer_stats_history").upsert(
            records, on_conflict="influencer_id,snapshot_date"
        ).execute()

    logger.info(f"Snapshotted stats for {len(records)} influencers on {today}")
    return len(records)
