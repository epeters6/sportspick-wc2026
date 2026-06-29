"""
Calibration harness — tracks prediction accuracy over time.

Metrics computed:
  Brier score     — mean squared error between confidence and binary outcome.
  Hit rate        — accuracy bucketed by confidence level (1D) and by confidence
                    × market price (2D) when market_prob_at_pick is available.
  Upset trap      — high consensus confidence + low market price hit rate.
  ROI (simulated) — average return on a flat-bet strategy at implied odds.

Results are written to the calibration_logs table for trend tracking, and
also returned as a dict for the sync log.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from loguru import logger

from backend.db import get_db

CONFIDENCE_BUCKETS = [
    (0.0, 0.50, "low"),
    (0.50, 0.65, "medium-low"),
    (0.65, 0.80, "medium-high"),
    (0.80, 1.01, "high"),
]

MARKET_BUCKETS = [
    (0.0, 0.15, "longshot"),
    (0.15, 0.35, "underdog"),
    (0.35, 0.55, "coinflip"),
    (0.55, 1.01, "favorite"),
]

UPSET_CONF_MIN = 0.65
UPSET_MARKET_MAX = 0.20


def _bucket(confidence: float) -> str:
    for lo, hi, label in CONFIDENCE_BUCKETS:
        if lo <= confidence < hi:
            return label
    return "high"


def _market_bucket(market_prob: float) -> str:
    p = max(0.0, min(market_prob, 1.0))
    for lo, hi, label in MARKET_BUCKETS:
        if lo <= p < hi:
            return label
    return "coinflip"


def _implied_decimal_odds(confidence: float) -> float:
    if confidence <= 0 or confidence >= 1:
        return 1.0
    return 1.0 / confidence


def compute_brier_score(picks: list[dict]) -> float:
    """Brier score = mean((confidence - outcome_binary)^2)."""
    if not picks:
        return 0.0
    total = sum(
        ((p.get("confidence") or 0.5) - (1.0 if p["outcome"] == "correct" else 0.0)) ** 2
        for p in picks
    )
    return round(total / len(picks), 4)


def _hit_rate_stats(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"hit_rate": 0.0, "correct": 0, "total": 0}
    wins = sum(1 for r in rows if r["outcome"] == "correct")
    return {
        "hit_rate": round(wins / len(rows), 4),
        "correct": wins,
        "total": len(rows),
    }


def run_calibration(*, persist: bool = True) -> dict[str, Any]:
    """Compute calibration metrics. Set persist=False for fast read-only API responses."""
    return _compute_calibration(persist=persist)


def get_calibration_summary() -> dict[str, Any]:
    """Public API — read-only stats (no DB writes on dashboard refresh)."""
    return _compute_calibration(persist=False)


def _compute_calibration(*, persist: bool) -> dict[str, Any]:
    db = get_db()
    resolved = (
        db.table("picks")
        .select(
            "id, match_id, predicted_winner, confidence, outcome, bet_type, "
            "market_prob_at_pick"
        )
        .in_("outcome", ["correct", "incorrect"])
        .execute()
        .data or []
    )

    if not resolved:
        logger.info("Calibration: no resolved picks yet")
        return {
            "total_resolved": 0,
            "brier_score": 0.0,
            "simulated_roi_pct": 0.0,
            "hit_rates_by_bucket": {},
            "hit_rates_by_bet_type": {},
            "hit_rates_2d": {},
            "upset_trap": {},
        }

    overall_brier = compute_brier_score(resolved)

    # ── 1D hit rate by confidence bucket ─────────────────────────────────────
    bucket_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    for p in resolved:
        label = _bucket(p.get("confidence") or 0.5)
        bucket_stats[label]["total"] += 1
        if p["outcome"] == "correct":
            bucket_stats[label]["correct"] += 1

    hit_rates = {
        label: {
            "hit_rate": round(v["correct"] / v["total"], 4) if v["total"] else 0.0,
            "correct": v["correct"],
            "total": v["total"],
        }
        for label, v in bucket_stats.items()
    }

    # ── 2D: confidence × market price ────────────────────────────────────────
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    picks_with_market = [p for p in resolved if p.get("market_prob_at_pick") is not None]
    for p in picks_with_market:
        conf_label = _bucket(p.get("confidence") or 0.5)
        mkt_label = _market_bucket(p["market_prob_at_pick"])
        matrix.setdefault(conf_label, {}).setdefault(mkt_label, []).append(p)

    hit_rates_2d: dict[str, dict[str, dict[str, Any]]] = {}
    for conf_label, mkt_map in matrix.items():
        hit_rates_2d[conf_label] = {}
        for mkt_label, cell_rows in mkt_map.items():
            hit_rates_2d[conf_label][mkt_label] = _hit_rate_stats(cell_rows)

    # ── Upset trap on picks (high confidence, low market) ────────────────────
    trap_rows = [
        p for p in picks_with_market
        if (p.get("confidence") or 0) >= UPSET_CONF_MIN
        and (p.get("market_prob_at_pick") or 1) < UPSET_MARKET_MAX
    ]
    normal_rows = [
        p for p in picks_with_market
        if not (
            (p.get("confidence") or 0) >= UPSET_CONF_MIN
            and (p.get("market_prob_at_pick") or 1) < UPSET_MARKET_MAX
        )
    ]
    upset_trap = {
        "upset_trap": {**_hit_rate_stats(trap_rows), "label": "High conf + low market"},
        "normal": {**_hit_rate_stats(normal_rows), "label": "All other picks"},
    }

    # ── Simulated ROI ────────────────────────────────────────────────────────
    total_bet = len(resolved)
    total_return = 0.0
    for p in resolved:
        conf = p.get("confidence") or 0.5
        odds = _implied_decimal_odds(conf)
        if p["outcome"] == "correct":
            total_return += odds
    simulated_roi = round((total_return - total_bet) / total_bet * 100, 2) if total_bet else 0.0

    # ── Bet-type breakdown ───────────────────────────────────────────────────
    type_stats: dict[str, dict] = defaultdict(lambda: {"correct": 0, "total": 0})
    for p in resolved:
        bt = p.get("bet_type") or "moneyline"
        type_stats[bt]["total"] += 1
        if p["outcome"] == "correct":
            type_stats[bt]["correct"] += 1
    bet_type_rates = {
        bt: {
            "hit_rate": round(v["correct"] / v["total"], 4) if v["total"] else 0.0,
            **v,
        }
        for bt, v in type_stats.items()
    }

    # ── Persist calibration logs ─────────────────────────────────────────────
    logs = []
    for p in resolved:
        conf = p.get("confidence") or 0.5
        is_correct = p["outcome"] == "correct"
        brier_contrib = (conf - (1.0 if is_correct else 0.0)) ** 2
        logs.append({
            "match_id": p.get("match_id"),
            "bet_type": p.get("bet_type") or "moneyline",
            "predicted_outcome": p.get("predicted_winner"),
            "confidence": conf,
            "actual_outcome": p["outcome"],
            "brier_contribution": round(brier_contrib, 6),
            "is_correct": is_correct,
        })

    if logs and persist:
        # One row per match+outcome (multiple pickers may share the same key)
        deduped: dict[tuple, dict] = {}
        for row in logs:
            key = (row.get("match_id"), row.get("predicted_outcome"))
            deduped[key] = row
        logs = list(deduped.values())
        try:
            db.table("calibration_logs").upsert(
                logs, on_conflict="match_id,predicted_outcome"
            ).execute()
        except Exception as exc:
            logger.debug(f"Calibration log upsert partial failure: {exc}")

    summary = {
        "total_resolved": len(resolved),
        "brier_score": overall_brier,
        "simulated_roi_pct": simulated_roi,
        "hit_rates_by_bucket": hit_rates,
        "hit_rates_by_bet_type": bet_type_rates,
        "hit_rates_2d": hit_rates_2d,
        "upset_trap": upset_trap,
        "picks_with_market_line": len(picks_with_market),
    }

    logger.info(
        f"Calibration: {len(resolved)} resolved picks | "
        f"Brier={overall_brier:.4f} | ROI={simulated_roi:.1f}% | "
        f"2D cells={sum(len(v) for v in hit_rates_2d.values())}"
    )
    return summary


def get_calibration_summary() -> dict[str, Any]:
    """Public API — returns latest calibration stats (used by the dashboard API)."""
    return run_calibration()
