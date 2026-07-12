"""
World Cup quant model — team-strength Elo + draw model for 3-way football markets.

Pipeline:
  1. build_team_elos(): replay all finished football matches in the `matches`
     table chronologically, updating a per-team Elo (K=32, goal-diff multiplier).
  2. predict_match(): convert an Elo gap into a 3-way (home/draw/away)
     probability distribution. Draw probability decays with rating gap —
     evenly matched teams draw more often.
  3. sync_wc_quant_predictions(): write per-outcome rows to `model_predictions`
     (source='wc_quant') for upcoming football matches so the consensus engine
     can blend them with the influencer crowd.

Safety: predictions are only emitted when BOTH teams have at least
MIN_TEAM_MATCHES finished matches in the DB — a cold-start Elo of 1500 vs 1500
would just echo a coin flip and pollute the blend.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from backend.db import get_db

ELO_START = 1500.0
ELO_K = 32.0
MIN_TEAM_MATCHES = 3          # both teams need this much history to predict
PREDICT_HORIZON_DAYS = 14     # only predict matches starting within this window

# Draw model: base draw rate for evenly matched international sides (~28%),
# decaying as the Elo gap grows (mismatches rarely end level).
DRAW_BASE = 0.28
DRAW_DECAY_SCALE = 600.0

# WC 2026 hosts get a modest home-crowd bump when playing in their country.
HOST_NATIONS = {"united states", "usa", "canada", "mexico"}
HOST_ELO_BONUS = 50.0

SOURCE = "wc_quant"


def _expected(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, (elo_b - elo_a) / 400.0))


def _goal_diff_multiplier(diff: int) -> float:
    """FiveThirtyEight-style margin-of-victory multiplier."""
    if diff <= 1:
        return 1.0
    if diff == 2:
        return 1.5
    return (11.0 + diff) / 8.0


def build_team_elos(db=None) -> tuple[dict[str, float], dict[str, int]]:
    """Replay finished football matches chronologically into team Elo ratings.

    Returns (elos, match_counts) keyed by lowercase team name.
    """
    db = db or get_db()
    rows = (
        db.table("matches")
        .select("home_team, away_team, home_score, away_score, winner, scheduled_at")
        .eq("sport", "football")
        .eq("is_final", True)
        .order("scheduled_at")
        .execute()
        .data or []
    )

    elos: dict[str, float] = {}
    counts: dict[str, int] = {}

    for m in rows:
        home = (m.get("home_team") or "").strip().lower()
        away = (m.get("away_team") or "").strip().lower()
        if not home or not away:
            continue
        hs, as_ = m.get("home_score"), m.get("away_score")
        winner = (m.get("winner") or "").strip().lower()
        if hs is None or as_ is None:
            # Fall back to the winner column when scores are missing
            if winner == home:
                hs, as_ = 1, 0
            elif winner == away:
                hs, as_ = 0, 1
            elif winner == "draw":
                hs, as_ = 0, 0
            else:
                continue

        eh = elos.get(home, ELO_START)
        ea = elos.get(away, ELO_START)

        if hs > as_:
            score_h = 1.0
        elif hs < as_:
            score_h = 0.0
        else:
            score_h = 0.5

        exp_h = _expected(eh, ea)
        k = ELO_K * _goal_diff_multiplier(abs(int(hs) - int(as_)))

        elos[home] = eh + k * (score_h - exp_h)
        elos[away] = ea + k * ((1.0 - score_h) - (1.0 - exp_h))
        counts[home] = counts.get(home, 0) + 1
        counts[away] = counts.get(away, 0) + 1

    logger.info(f"WC quant: built Elo for {len(elos)} teams from {len(rows)} finished matches")
    return elos, counts


def predict_match(
    home_team: str,
    away_team: str,
    elos: dict[str, float],
    *,
    venue_country: str | None = None,
) -> dict[str, float] | None:
    """3-way (home/draw/away) probabilities from team Elos.

    Returns None if either team is unrated.
    """
    home_key = home_team.strip().lower()
    away_key = away_team.strip().lower()
    if home_key not in elos or away_key not in elos:
        return None

    eh, ea = elos[home_key], elos[away_key]
    # Neutral-venue tournament: only genuine host nations get a bump.
    if home_key in HOST_NATIONS:
        eh += HOST_ELO_BONUS
    if away_key in HOST_NATIONS:
        ea += HOST_ELO_BONUS

    dr = eh - ea
    p_home_no_draw = _expected(eh, ea)
    p_draw = DRAW_BASE * math.exp(-((dr / DRAW_DECAY_SCALE) ** 2))
    p_home = (1.0 - p_draw) * p_home_no_draw
    p_away = (1.0 - p_draw) * (1.0 - p_home_no_draw)

    total = p_home + p_draw + p_away
    return {
        "home_prob": round(p_home / total, 4),
        "draw_prob": round(p_draw / total, 4),
        "away_prob": round(p_away / total, 4),
        "elo_home": round(eh, 1),
        "elo_away": round(ea, 1),
    }


def get_wc_quant_probability(
    home_team: str,
    away_team: str,
    db=None,
) -> dict[str, float] | None:
    """One-shot prediction used by the consensus engine blend (mirrors the MLB API)."""
    db = db or get_db()
    elos, counts = build_team_elos(db)
    home_key = home_team.strip().lower()
    away_key = away_team.strip().lower()
    if counts.get(home_key, 0) < MIN_TEAM_MATCHES or counts.get(away_key, 0) < MIN_TEAM_MATCHES:
        return None
    return predict_match(home_team, away_team, elos)


def sync_wc_quant_predictions(db=None) -> int:
    """Write wc_quant model_predictions rows for upcoming football matches."""
    db = db or get_db()
    elos, counts = build_team_elos(db)
    if not elos:
        logger.info("WC quant: no finished football matches yet — skipping predictions")
        return 0

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=PREDICT_HORIZON_DAYS)
    upcoming = (
        db.table("matches")
        .select("id, home_team, away_team, scheduled_at, stage, venue")
        .eq("sport", "football")
        .eq("is_final", False)
        .gte("scheduled_at", now.isoformat())
        .lte("scheduled_at", horizon.isoformat())
        .execute()
        .data or []
    )

    written = 0
    for m in upcoming:
        home, away = m.get("home_team") or "", m.get("away_team") or ""
        if not home or not away:
            continue
        if counts.get(home.strip().lower(), 0) < MIN_TEAM_MATCHES:
            continue
        if counts.get(away.strip().lower(), 0) < MIN_TEAM_MATCHES:
            continue

        pred = predict_match(home, away, elos)
        if not pred:
            continue

        match_id = m["id"]
        meta = {
            "elo_home": pred["elo_home"],
            "elo_away": pred["elo_away"],
            "model": "elo_draw_v1",
        }
        rows = [
            {"source": SOURCE, "domain": "sports", "event_key": match_id,
             "outcome": home, "prob": pred["home_prob"], "metadata": meta},
            {"source": SOURCE, "domain": "sports", "event_key": match_id,
             "outcome": "draw", "prob": pred["draw_prob"], "metadata": meta},
            {"source": SOURCE, "domain": "sports", "event_key": match_id,
             "outcome": away, "prob": pred["away_prob"], "metadata": meta},
        ]
        try:
            db.table("model_predictions").delete().eq("source", SOURCE).eq("event_key", match_id).execute()
            db.table("model_predictions").insert(rows).execute()
            written += 1
        except Exception as exc:
            logger.warning(f"WC quant: failed to write predictions for {home} v {away}: {exc}")

    logger.info(f"WC quant: wrote predictions for {written}/{len(upcoming)} upcoming matches")
    return written


def resolve_wc_quant_predictions(db=None) -> int:
    """Grade unresolved wc_quant predictions against final match results."""
    db = db or get_db()
    pending = (
        db.table("model_predictions")
        .select("id, event_key, outcome, prob")
        .eq("source", SOURCE)
        .is_("resolved_at", "null")
        .execute()
        .data or []
    )
    if not pending:
        return 0

    match_ids = list({p["event_key"] for p in pending})
    resolved = 0
    for i in range(0, len(match_ids), 100):
        chunk = match_ids[i:i + 100]
        finals = (
            db.table("matches")
            .select("id, winner, is_final")
            .in_("id", chunk)
            .eq("is_final", True)
            .execute()
            .data or []
        )
        winners = {m["id"]: (m.get("winner") or "").strip().lower() for m in finals}
        for p in pending:
            w = winners.get(p["event_key"])
            if not w:
                continue
            is_correct = p["outcome"].strip().lower() == w
            try:
                db.table("model_predictions").update({
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                    "is_correct": is_correct,
                }).eq("id", p["id"]).execute()
                resolved += 1
            except Exception as exc:
                logger.debug(f"WC quant: resolve failed for prediction {p['id']}: {exc}")

    logger.info(f"WC quant: resolved {resolved} predictions")
    return resolved


if __name__ == "__main__":
    sync_wc_quant_predictions()
    resolve_wc_quant_predictions()
