"""
Calibration harness — tracks prediction accuracy over time.

Metrics computed:
  Brier score (calibrated) — primary metric using empirical hit rates, not raw scraper defaults.
  Brier score (raw)        — legacy metric on stored confidence (usually overconfident).
  Hit rate                 — bucketed by confidence level and bet type.
  ROI (simulated)          — flat-stake return at calibrated implied odds.

Results are written to the calibration_logs table for trend tracking.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from loguru import logger

from backend.db import get_db
from backend.trading.edge_model import (
    MONEYLINE_BET_TYPES,
    MLB_MIN_CALIBRATION_SAMPLES,
    _load_calibration_curve,
    _mlb_match_ids,
    calibrate_confidence,
)

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


def compute_brier_score(picks: list[dict], *, confidence_key: str = "confidence") -> float:
    """Brier score = mean((confidence - outcome_binary)^2)."""
    if not picks:
        return 0.0
    total = sum(
        ((p.get(confidence_key) or 0.5) - (1.0 if p["outcome"] == "correct" else 0.0)) ** 2
        for p in picks
    )
    return round(total / len(picks), 4)


def _attach_calibrated_confidence(
    picks: list[dict],
    curve_1d: dict,
    curve_2d: dict,
) -> list[dict]:
    """Return copies with calibrated_confidence filled in."""
    out = []
    for p in picks:
        raw = p.get("confidence") or 0.5
        calibrated = calibrate_confidence(
            raw,
            p.get("market_prob_at_pick"),
            curve_1d=curve_1d,
            curve_2d=curve_2d,
        )
        out.append({**p, "calibrated_confidence": round(calibrated, 4)})
    return out


def _segment_metrics(
    picks: list[dict],
    curve_1d: dict,
    curve_2d: dict,
) -> dict[str, Any]:
    if not picks:
        return {
            "total_resolved": 0,
            "hit_rate": 0.0,
            "brier_score": 0.0,
            "raw_brier_score": 0.0,
            "calibrated_brier_score": 0.0,
        }
    calibrated = _attach_calibrated_confidence(picks, curve_1d, curve_2d)
    wins = sum(1 for p in picks if p["outcome"] == "correct")
    return {
        "total_resolved": len(picks),
        "hit_rate": round(wins / len(picks), 4),
        "brier_score": compute_brier_score(calibrated, confidence_key="calibrated_confidence"),
        "raw_brier_score": compute_brier_score(picks),
        "calibrated_brier_score": compute_brier_score(calibrated, confidence_key="calibrated_confidence"),
    }


def _hit_rate_stats(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"hit_rate": 0.0, "correct": 0, "total": 0}
    wins = sum(1 for r in rows if r["outcome"] == "correct")
    return {
        "hit_rate": round(wins / len(rows), 4),
        "correct": wins,
        "total": len(rows),
    }


def _curve_for_display(curve_1d: dict) -> dict[str, float]:
    """Human-readable bucket label → empirical hit rate."""
    labels = {label: (lo, hi) for lo, hi, label in CONFIDENCE_BUCKETS}
    out: dict[str, float] = {}
    for label, (lo, hi) in labels.items():
        rate = curve_1d.get((lo, hi))
        if rate is not None:
            out[label] = round(rate, 4)
    return out


def run_calibration(*, persist: bool = True) -> dict[str, Any]:
    """Compute calibration metrics. Set persist=False for fast read-only API responses."""
    return _compute_calibration(persist=persist)


def get_calibration_summary() -> dict[str, Any]:
    """Public API — read-only stats (no DB writes on dashboard refresh)."""
    return _compute_calibration(persist=False)


def _compute_calibration(*, persist: bool) -> dict[str, Any]:
    db = get_db()
    curve_1d, curve_2d, ml_history = _load_calibration_curve()
    mlb_curve_1d, mlb_curve_2d, mlb_history = _load_calibration_curve("mlb")
    mlb_ids = _mlb_match_ids()

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
            "raw_brier_score": 0.0,
            "calibrated_brier_score": 0.0,
            "simulated_roi_pct": 0.0,
            "hit_rates_by_bucket": {},
            "hit_rates_by_bet_type": {},
            "hit_rates_2d": {},
            "upset_trap": {},
            "moneyline": {},
            "props": {},
            "mlb": {},
            "calibration_curve": {},
            "ml_history_size": ml_history,
        }

    ml_picks = [p for p in resolved if (p.get("bet_type") or "moneyline") in MONEYLINE_BET_TYPES]
    mlb_ml_picks = [p for p in ml_picks if p.get("match_id") in mlb_ids]
    prop_picks = [p for p in resolved if (p.get("bet_type") or "moneyline") not in MONEYLINE_BET_TYPES]
    calibrated_all = _attach_calibrated_confidence(resolved, curve_1d, curve_2d)

    # Primary headline metrics use moneyline picks (what we bet on).
    ml_metrics = _segment_metrics(ml_picks, curve_1d, curve_2d)
    mlb_metrics = _segment_metrics(mlb_ml_picks, mlb_curve_1d, mlb_curve_2d) if mlb_ml_picks else {}
    prop_metrics = _segment_metrics(prop_picks, curve_1d, curve_2d) if prop_picks else {}

    overall_brier = ml_metrics["calibrated_brier_score"]
    raw_brier = ml_metrics["raw_brier_score"]

    # ── 1D hit rate by *calibrated* confidence bucket (moneyline) ────────────
    bucket_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    ml_calibrated = _attach_calibrated_confidence(ml_picks, curve_1d, curve_2d)
    for p in ml_calibrated:
        label = _bucket(p.get("calibrated_confidence") or 0.5)
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

    # ── 2D: calibrated confidence × market price ─────────────────────────────
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    picks_with_market = [p for p in ml_calibrated if p.get("market_prob_at_pick") is not None]
    for p in picks_with_market:
        conf_label = _bucket(p.get("calibrated_confidence") or 0.5)
        mkt_label = _market_bucket(p["market_prob_at_pick"])
        matrix.setdefault(conf_label, {}).setdefault(mkt_label, []).append(p)

    hit_rates_2d: dict[str, dict[str, dict[str, Any]]] = {}
    for conf_label, mkt_map in matrix.items():
        hit_rates_2d[conf_label] = {}
        for mkt_label, cell_rows in mkt_map.items():
            hit_rates_2d[conf_label][mkt_label] = _hit_rate_stats(cell_rows)

    # ── Upset trap (calibrated conf + low market) ────────────────────────────
    trap_rows = [
        p for p in picks_with_market
        if (p.get("calibrated_confidence") or 0) >= UPSET_CONF_MIN
        and (p.get("market_prob_at_pick") or 1) < UPSET_MARKET_MAX
    ]
    normal_rows = [
        p for p in picks_with_market
        if not (
            (p.get("calibrated_confidence") or 0) >= UPSET_CONF_MIN
            and (p.get("market_prob_at_pick") or 1) < UPSET_MARKET_MAX
        )
    ]
    upset_trap = {
        "upset_trap": {**_hit_rate_stats(trap_rows), "label": "High conf + low market"},
        "normal": {**_hit_rate_stats(normal_rows), "label": "All other picks"},
    }

    # ── Simulated ROI at calibrated odds (moneyline) ─────────────────────────
    total_bet = len(ml_calibrated)
    total_return = 0.0
    for p in ml_calibrated:
        conf = p.get("calibrated_confidence") or 0.5
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

    # ── Persist calibration logs (calibrated confidence) ─────────────────────
    logs = []
    for p in calibrated_all:
        conf = p.get("calibrated_confidence") or 0.5
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
        "raw_brier_score": raw_brier,
        "calibrated_brier_score": overall_brier,
        "simulated_roi_pct": simulated_roi,
        "hit_rates_by_bucket": hit_rates,
        "hit_rates_by_bet_type": bet_type_rates,
        "hit_rates_2d": hit_rates_2d,
        "upset_trap": upset_trap,
        "picks_with_market_line": len(picks_with_market),
        "moneyline": ml_metrics,
        "props": prop_metrics,
        "mlb": {
            **mlb_metrics,
            "calibration_curve": _curve_for_display(mlb_curve_1d),
            "ml_history_size": mlb_history,
            "using_sport_curve": len(mlb_ml_picks) >= MLB_MIN_CALIBRATION_SAMPLES,
        },
        "calibration_curve": _curve_for_display(curve_1d),
        "ml_history_size": ml_history,
    }

    logger.info(
        f"Calibration: {len(resolved)} resolved | "
        f"Brier raw={raw_brier:.4f} calibrated={overall_brier:.4f} | "
        f"MLB n={len(mlb_ml_picks)} brier={mlb_metrics.get('calibrated_brier_score', 0):.4f} | "
        f"ROI={simulated_roi:.1f}% | ml_history={ml_history}"
    )
    return summary
