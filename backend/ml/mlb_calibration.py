"""Learn a conservative MLB model/market blend from all-game outcomes."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from loguru import logger

from backend.ml.prediction_evaluation import MLB_PREDICTION_SOURCE


@dataclass(frozen=True)
class MlbBlendCalibration:
    model_weight: float
    sample_rows: int
    sample_events: int
    raw_brier: float | None
    market_brier: float | None
    unconstrained_weight: float | None
    status: str

    def as_metadata(self) -> dict[str, Any]:
        return {
            "model_weight": self.model_weight,
            "blend_sample_rows": self.sample_rows,
            "blend_sample_events": self.sample_events,
            "blend_raw_brier": self.raw_brier,
            "blend_market_brier": self.market_brier,
            "blend_unconstrained_weight": self.unconstrained_weight,
            "blend_calibration_status": self.status,
        }


def learn_mlb_market_blend(
    db,
    *,
    fallback_weight: float = 0.10,
    min_events: int = 100,
    max_model_weight: float = 0.25,
    row_limit: int = 1000,
) -> MlbBlendCalibration:
    """Minimize historical Brier loss for ``w*model + (1-w)*market``.

    The fit uses every resolved game side, never only selected bets. The weight
    is capped because the raw MLB heuristic is not yet independently calibrated.
    """
    fallback = min(max(float(fallback_weight), 0.0), float(max_model_weight))
    try:
        rows = (
            db.table("model_predictions")
            .select("event_key,prob,market_price,is_correct")
            .eq("source", MLB_PREDICTION_SOURCE)
            .not_.is_("resolved_at", "null")
            .order("resolved_at", desc=True)
            .limit(max(1, int(row_limit)))
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning("MLB blend calibration lookup failed: {}", exc)
        return MlbBlendCalibration(
            model_weight=fallback,
            sample_rows=0,
            sample_events=0,
            raw_brier=None,
            market_brier=None,
            unconstrained_weight=None,
            status="fallback_database_error",
        )

    samples: list[tuple[float, float, float, str]] = []
    for row in rows:
        try:
            raw = float(row.get("prob"))
            market = float(row.get("market_price"))
        except (TypeError, ValueError):
            continue
        correct = row.get("is_correct")
        if correct is None or not all(math.isfinite(value) for value in (raw, market)):
            continue
        if not (0.001 <= raw <= 0.999 and 0.001 <= market <= 0.999):
            continue
        samples.append((raw, market, 1.0 if correct is True else 0.0, str(row.get("event_key") or "")))

    sample_events = len({sample[3] for sample in samples if sample[3]})
    raw_brier = (
        sum((raw - actual) ** 2 for raw, _market, actual, _key in samples) / len(samples)
        if samples
        else None
    )
    market_brier = (
        sum((market - actual) ** 2 for _raw, market, actual, _key in samples) / len(samples)
        if samples
        else None
    )
    denominator = sum((raw - market) ** 2 for raw, market, _actual, _key in samples)
    unconstrained = (
        sum(
            (raw - market) * (actual - market)
            for raw, market, actual, _key in samples
        )
        / denominator
        if denominator > 1e-12
        else None
    )
    if sample_events < max(1, int(min_events)) or unconstrained is None:
        return MlbBlendCalibration(
            model_weight=fallback,
            sample_rows=len(samples),
            sample_events=sample_events,
            raw_brier=raw_brier,
            market_brier=market_brier,
            unconstrained_weight=unconstrained,
            status="fallback_insufficient_history",
        )

    learned = min(float(max_model_weight), max(0.0, unconstrained))
    return MlbBlendCalibration(
        model_weight=learned,
        sample_rows=len(samples),
        sample_events=sample_events,
        raw_brier=raw_brier,
        market_brier=market_brier,
        unconstrained_weight=unconstrained,
        status="learned_all_game_brier_v1",
    )
