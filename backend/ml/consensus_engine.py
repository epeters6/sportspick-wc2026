"""
Consensus engine — aggregates all influencer picks for upcoming matches
and produces a weighted recommendation.

Improvements over v1:
  - Non-linear Elo weighting: uses elo^2 / ELO_DEFAULT^2 so a 1200-Elo
    picker gets 44% more influence than a 1000-Elo picker (not 20%).
  - Rookie penalty: influencers with < MIN_RESOLVED_PICKS resolved picks
    are down-weighted by ROOKIE_PENALTY to reduce noise from new accounts.
  - Full probability distribution: stores P(home_team), P(draw), P(away_team)
    alongside the consensus winner and pick_count.
  - pick_count: total number of picks that fed into this consensus record.
"""
from __future__ import annotations

import math
from collections import defaultdict

from loguru import logger

from backend.config import get_settings
from backend.db import get_db

ELO_DEFAULT = 1000.0
MIN_PICKS_FOR_CONSENSUS = 3   # default; overridden by settings at runtime


def _consensus_min_picks(sport: str | None = None) -> int:
    s = get_settings()
    if sport == "mlb":
        return s.consensus_min_picks_mlb
    if not s.polymarket_live_enabled:
        return s.consensus_min_picks_paper
    return s.consensus_min_picks
MIN_RESOLVED_PICKS = 5        # fewer than this → rookie penalty applied
ROOKIE_PENALTY = 0.5          # rookie picks count for half their normal weight
MIN_CLV_SAMPLES = 5           # need this many CLV picks before weighting kicks in


def _elo_weight(elo: float) -> float:
    """
    Non-linear Elo weight.  At ELO_DEFAULT → 1.0.  At 1200 → 1.44.  At 800 → 0.64.
    Squares the normalised ratio so high-Elo pickers have a more pronounced edge.
    """
    ratio = elo / ELO_DEFAULT
    return ratio * ratio


def _clv_weight(avg_clv: float | None, clv_samples: int, *, sport: str | None = None) -> float:
    """
    Pickers who consistently beat the closing line get more consensus weight.
    avg_clv is stored on influencers (actual - market_prob_at_pick per pick).
    """
    if avg_clv is None or clv_samples < MIN_CLV_SAMPLES:
        return 1.0
    settings = get_settings()
    scale = settings.clv_weight_scale
    if (sport or "").lower() == "mlb":
        scale = settings.clv_weight_scale_mlb
    return max(0.5, min(1.5, 1.0 + avg_clv * scale))


def _sport_elo(inf_data: dict, sport: str) -> float:
    """Prefer per-sport Elo when available."""
    by_sport = inf_data.get("elo_by_sport") or {}
    if isinstance(by_sport, dict) and sport in by_sport:
        return float(by_sport[sport] or ELO_DEFAULT)
    return float(inf_data.get("elo") or ELO_DEFAULT)


def _sport_avg_clv(inf_data: dict, sport: str) -> float | None:
    by_sport = inf_data.get("avg_clv_by_sport") or {}
    if isinstance(by_sport, dict) and sport in by_sport:
        val = by_sport.get(sport)
        return float(val) if val is not None else None
    return inf_data.get("avg_clv")


def _sp_pick_boost(
    pick: dict,
    *,
    sport: str,
    home_team: str,
    away_team: str,
    match_stats: dict | None,
) -> float:
    """Boost MLB moneyline picks aligned with probable-SP matchup edge."""
    if sport != "mlb" or not match_stats:
        return 1.0
    from backend.sports_data.mlb_stats_fetcher import sp_matchup_favored_team

    favored = sp_matchup_favored_team(match_stats)
    if not favored:
        return 1.0
    team = (pick.get("predicted_winner") or "").strip()
    if team == favored:
        return 1.12
    raw = (pick.get("raw_text") or "").lower()
    pitchers = (match_stats.get("probable_pitchers") or {})
    for pdata in pitchers.values():
        name = (pdata.get("name") or "").lower()
        if name and name in raw and pdata.get("team") == team:
            return 1.08
    return 1.0


def compute_consensus_for_match(match_id: str) -> dict | None:
    """
    For a given match, aggregate all pending moneyline/draw picks and produce
    a consensus with full P(home)/P(draw)/P(away) distribution.
    Returns the consensus record, or None if insufficient data.
    """
    db = get_db()

    match_row = (
        db.table("matches")
        .select("home_team, away_team, sport, match_stats")
        .eq("id", match_id)
        .single()
        .execute()
        .data
    )
    home_team = match_row.get("home_team", "") if match_row else ""
    away_team = match_row.get("away_team", "") if match_row else ""
    sport = (match_row or {}).get("sport", "football")
    match_stats = (match_row or {}).get("match_stats")

    # Fetch pending picks; filter moneyline/draw in Python (legacy rows have null bet_type)
    raw_picks = (
        db.table("picks")
        .select("predicted_winner, confidence, influencer_id, bet_type, raw_text")
        .eq("match_id", match_id)
        .eq("outcome", "pending")
        .execute()
        .data or []
    )
    picks = [
        p for p in raw_picks
        if (p.get("bet_type") or "moneyline") in ("moneyline", "draw")
    ]

    min_picks = _consensus_min_picks(sport)
    if len(picks) < min_picks:
        return None

    # Fetch influencer stats (Elo + resolved pick count for rookie check)
    influencer_ids = list({p["influencer_id"] for p in picks})
    influencers = (
        db.table("influencers")
        .select("id, elo_score, elo_by_sport, correct_picks, total_picks, avg_clv, avg_clv_by_sport")
        .in_("id", influencer_ids)
        .execute()
        .data or []
    )
    inf_map = {
        inf["id"]: {
            "elo": inf.get("elo_score") or ELO_DEFAULT,
            "elo_by_sport": inf.get("elo_by_sport") or {},
            "resolved": (inf.get("correct_picks") or 0) + max(
                0, (inf.get("total_picks") or 0) - (inf.get("correct_picks") or 0)
            ),
            "avg_clv": inf.get("avg_clv"),
            "avg_clv_by_sport": inf.get("avg_clv_by_sport") or {},
        }
        for inf in influencers
    }

    # Accumulate weighted votes per outcome
    vote_weights: dict[str, float] = defaultdict(float)
    vote_counts: dict[str, int] = defaultdict(int)
    top_supporters: dict[str, list[tuple[float, str]]] = defaultdict(list)

    for pick in picks:
        team = pick.get("predicted_winner")
        if not team:
            continue
        if team == "draw" and sport == "mlb":
            continue
        valid_teams = {home_team.strip().lower(), away_team.strip().lower()}
        if team.strip().lower() not in valid_teams and team != "draw":
            from backend.trading.market_matcher import _canonical
            canon = (_canonical(team) or team).strip().lower()
            home_c = (_canonical(home_team) or home_team).strip().lower()
            away_c = (_canonical(away_team) or away_team).strip().lower()
            if canon not in (home_c, away_c):
                continue
        conf = pick.get("confidence") or 0.55
        inf_data = inf_map.get(
            pick["influencer_id"],
            {"elo": ELO_DEFAULT, "resolved": 0, "avg_clv": None, "elo_by_sport": {}, "avg_clv_by_sport": {}},
        )
        elo_w = _elo_weight(_sport_elo(inf_data, sport))
        rookie_w = 1.0 if inf_data["resolved"] >= MIN_RESOLVED_PICKS else ROOKIE_PENALTY
        clv_w = _clv_weight(
            _sport_avg_clv(inf_data, sport),
            inf_data["resolved"],
            sport=sport,
        )
        sp_w = _sp_pick_boost(
            pick,
            sport=sport,
            home_team=home_team,
            away_team=away_team,
            match_stats=match_stats,
        )
        weight = elo_w * conf * rookie_w * clv_w * sp_w
        vote_weights[team] += weight
        vote_counts[team] += 1
        top_supporters[team].append((weight, pick["influencer_id"]))

    if not vote_weights:
        return None

    total_weight = sum(vote_weights.values())
    best_team = max(vote_weights, key=lambda t: vote_weights[t])
    confidence = vote_weights[best_team] / total_weight if total_weight > 0 else 0.0

    # Full probability distribution
    def _prob(team: str) -> float:
        if not total_weight:
            return 0.0
        return round(vote_weights.get(team, 0.0) / total_weight, 4)

    home_probability = _prob(home_team)
    draw_probability = _prob("draw")
    away_probability = _prob(away_team)

    # Top 5 influencers backing the consensus pick
    supporters = sorted(top_supporters[best_team], key=lambda x: x[0], reverse=True)
    top_5_ids = [s[1] for s in supporters[:5]]

    record = {
        "match_id": match_id,
        "predicted_winner": best_team,
        "bet_type": "moneyline",
        "bet_line": None,
        "consensus_key": f"moneyline|{best_team}|",
        "total_votes": vote_counts[best_team],
        "weighted_score": round(vote_weights[best_team], 4),
        "confidence": round(confidence, 4),
        "top_influencers": top_5_ids,
        "pick_count": len(picks),
        "home_probability": home_probability,
        "draw_probability": draw_probability,
        "away_probability": away_probability,
    }

    try:
        db.table("consensus_picks").upsert(
            record, on_conflict="match_id,consensus_key"
        ).execute()
    except Exception as exc:
        logger.debug(f"Consensus upsert (match_id,consensus_key) fallback: {exc}")
        db.table("consensus_picks").upsert(
            record, on_conflict="match_id,predicted_winner"
        ).execute()

    return record


def compute_all_consensus() -> int:
    """Compute moneyline + prop consensus for all upcoming matches."""
    from backend.ml.consensus_props import compute_all_prop_consensus

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
    prop_n = compute_all_prop_consensus()
    logger.info(
        f"Computed consensus for {computed}/{len(matches)} matches "
        f"+ {prop_n} prop signals"
    )
    return computed + prop_n


def get_top_recommendations(limit: int = 10, sport: str | None = None) -> list[dict]:
    """
    Return the top N recommended picks across all upcoming matches,
    sorted by calibrated confidence (empirical win rate, not raw vote share).
    MLB picks use a sport-specific calibration curve when enough history exists.
    """
    from backend.trading.edge_model import _load_calibration_curve, calibrate_confidence

    global_curve = _load_calibration_curve("")
    mlb_curve = _load_calibration_curve("mlb")

    db = get_db()
    fetch_limit = limit * 5 if sport else limit
    rows = (
        db.table("consensus_picks")
        .select("*, matches(home_team, away_team, scheduled_at, stage, sport)")
        .order("confidence", desc=True)
        .limit(fetch_limit)
        .execute()
        .data or []
    )
    if sport:
        rows = [r for r in rows if (r.get("matches") or {}).get("sport") == sport]

    enriched: list[dict] = []
    for r in rows:
        raw = r.get("confidence") or 0.5
        match_sport = (r.get("matches") or {}).get("sport") or ""
        if match_sport == "mlb":
            curve_1d, curve_2d, _ = mlb_curve
        else:
            curve_1d, curve_2d, _ = global_curve
        calibrated = round(
            calibrate_confidence(raw, curve_1d=curve_1d, curve_2d=curve_2d),
            4,
        )
        enriched.append({
            **r,
            "raw_confidence": round(raw, 4),
            "calibrated_confidence": calibrated,
        })
    enriched.sort(key=lambda x: x.get("calibrated_confidence") or 0, reverse=True)
    return enriched[:limit]
