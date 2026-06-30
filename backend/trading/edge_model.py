"""
Edge model — turns a raw crowd-consensus into a *bettable* probability and
computes the edge against a live Polymarket price.

Why this is more than `edge = confidence - price`
--------------------------------------------------
Our consensus "confidence" is a weighted vote share, not a calibrated
probability. Sizing real money with Kelly on a miscalibrated probability is
the fastest way to ruin. So we apply three corrections, in order:

1. VIG REMOVAL — Polymarket binary prices for an event sum to slightly >1.
   Normalise them so the market's implied probabilities are vig-free.

2. CALIBRATION — Map raw consensus confidence to an empirically observed hit
   rate, learned from resolved-pick history. Uses 2D buckets (confidence ×
   market price) when enough data exists, falling back to 1D confidence buckets.

3. SHRINKAGE / BLEND — Blend the calibrated model probability toward the
   market probability. The model's weight grows with (a) how much resolved
   history we have and (b) how many independent pickers fed the consensus.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from backend.db import get_db

MIN_HISTORY_FOR_TRUST = 30
FULL_TRUST_HISTORY = 300
MAX_MODEL_WEIGHT = 0.50
FULL_TRUST_PICKERS = 12
MIN_2D_CELL_SAMPLES = 3
SHRINKAGE_PRIOR = 8  # pseudo-observations toward 50% for thin buckets

MONEYLINE_BET_TYPES = frozenset({"moneyline", "draw"})

_CONF_BUCKETS = [(0.0, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 1.01)]
_MKT_BUCKETS = [(0.0, 0.15), (0.15, 0.35), (0.35, 0.55), (0.55, 1.01)]


@dataclass
class EdgeResult:
    model_prob: float
    market_prob: float
    market_price: float
    raw_confidence: float
    edge: float
    model_weight: float
    note: str = ""


def remove_vig(prices: list[float]) -> list[float]:
    total = sum(p for p in prices if p and p > 0)
    if total <= 0:
        return prices
    return [(p / total if p else 0.0) for p in prices]


def _bucket_key(value: float, buckets: list[tuple[float, float]]) -> tuple[float, float]:
    for lo, hi in buckets:
        if lo <= value < hi:
            return (lo, hi)
    return buckets[-1]


def _shrunk_hit_rate(wins: int, total: int, *, prior: float = 0.5) -> float:
    """Bayesian shrinkage — thin buckets regress toward coin-flip."""
    if total <= 0:
        return prior
    return (wins + SHRINKAGE_PRIOR * prior) / (total + SHRINKAGE_PRIOR)


def _load_calibration_curve() -> tuple[
    dict[tuple[float, float], float],
    dict[tuple[float, float, float, float], float],
    int,
]:
    """
    Build 1D and 2D calibration curves from resolved picks.
    2D keys: (conf_lo, conf_hi, mkt_lo, mkt_hi) → empirical hit rate.
    """
    db = get_db()
    rows = (
        db.table("picks")
        .select("confidence, outcome, market_prob_at_pick, bet_type")
        .in_("outcome", ["correct", "incorrect"])
        .execute()
        .data or []
    )
    # Moneyline/draw only — props have different base rates and poison the curve.
    ml_rows = [
        r for r in rows
        if (r.get("bet_type") or "moneyline") in MONEYLINE_BET_TYPES
    ]
    total = len(ml_rows)

    curve_1d: dict[tuple[float, float], float] = {}
    for lo, hi in _CONF_BUCKETS:
        bucket_rows = [r for r in ml_rows if lo <= (r.get("confidence") or 0.5) < hi]
        if bucket_rows:
            wins = sum(1 for r in bucket_rows if r["outcome"] == "correct")
            curve_1d[(lo, hi)] = _shrunk_hit_rate(wins, len(bucket_rows))
        else:
            curve_1d[(lo, hi)] = 0.5

    curve_2d: dict[tuple[float, float, float, float], float] = {}
    for clo, chi in _CONF_BUCKETS:
        for mlo, mhi in _MKT_BUCKETS:
            cell = [
                r for r in ml_rows
                if clo <= (r.get("confidence") or 0.5) < chi
                and r.get("market_prob_at_pick") is not None
                and mlo <= r["market_prob_at_pick"] < mhi
            ]
            if len(cell) >= MIN_2D_CELL_SAMPLES:
                wins = sum(1 for r in cell if r["outcome"] == "correct")
                curve_2d[(clo, chi, mlo, mhi)] = _shrunk_hit_rate(wins, len(cell))

    return curve_1d, curve_2d, total


def calibrate_confidence(
    raw_confidence: float,
    market_price: float | None = None,
    curve_1d: dict | None = None,
    curve_2d: dict | None = None,
) -> float:
    """Map raw consensus confidence to empirical hit rate (2D when available)."""
    if curve_1d is None or curve_2d is None:
        curve_1d, curve_2d, _ = _load_calibration_curve()

    if market_price is not None:
        ck = _bucket_key(raw_confidence, _CONF_BUCKETS)
        mk = _bucket_key(market_price, _MKT_BUCKETS)
        key_2d = (ck[0], ck[1], mk[0], mk[1])
        if key_2d in curve_2d:
            return curve_2d[key_2d]

    for (lo, hi), rate in curve_1d.items():
        if lo <= raw_confidence < hi:
            return rate
    return raw_confidence


def _model_weight(history_size: int, picker_count: int) -> float:
    if history_size < MIN_HISTORY_FOR_TRUST:
        return 0.0
    hist_gate = min(
        1.0,
        (history_size - MIN_HISTORY_FOR_TRUST) / max(1, FULL_TRUST_HISTORY - MIN_HISTORY_FOR_TRUST),
    )
    signal_gate = min(1.0, picker_count / FULL_TRUST_PICKERS)
    return MAX_MODEL_WEIGHT * hist_gate * signal_gate


def compute_edge(
    raw_confidence: float,
    market_price: float,
    *,
    picker_count: int,
    fee_bps: float = 0.0,
    calibration_curve: dict | None = None,
    calibration_curve_2d: dict | None = None,
    history_size: int | None = None,
    paper_mode: bool = False,
    min_history_override: int | None = None,
    max_model_weight_override: float | None = None,
) -> EdgeResult:
    if calibration_curve is None or calibration_curve_2d is None or history_size is None:
        calibration_curve, calibration_curve_2d, history_size = _load_calibration_curve()

    calibrated = calibrate_confidence(
        raw_confidence,
        market_price,
        curve_1d=calibration_curve,
        curve_2d=calibration_curve_2d,
    )
    min_hist = min_history_override if min_history_override is not None else MIN_HISTORY_FOR_TRUST
    max_w = max_model_weight_override if max_model_weight_override is not None else MAX_MODEL_WEIGHT

    if paper_mode and history_size >= min_hist:
        hist_gate = min(
            1.0,
            (history_size - min_hist) / max(1, FULL_TRUST_HISTORY - min_hist),
        )
        signal_gate = min(1.0, picker_count / FULL_TRUST_PICKERS)
        w = max_w * hist_gate * signal_gate
    else:
        w = _model_weight(history_size, picker_count)
        if max_model_weight_override is not None:
            w = min(w, max_w)

    blended = w * calibrated + (1 - w) * market_price
    fee = fee_bps / 10_000.0
    edge = blended - market_price - fee

    note = ""
    if w == 0.0:
        note = f"model untrusted (history={history_size} < {min_hist})"

    return EdgeResult(
        model_prob=round(blended, 4),
        market_prob=round(market_price, 4),
        market_price=round(market_price, 4),
        raw_confidence=round(raw_confidence, 4),
        edge=round(edge, 4),
        model_weight=round(w, 4),
        note=note,
    )
