"""Backlog-trained calibration for weather execution candidates.

The core weather vector is retained for evaluation. This module only calibrates
the post-selection execution probability, where winner's-curse bias is largest.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from loguru import logger

from backend.ml.prediction_evaluation import WEATHER_PREDICTION_SOURCE

MODEL_VERSION = "weather_calibrated_v2"
PROBABILITY_BIN_WIDTH = 0.05
MIN_EXACT_COHORT = 8
MIN_DIRECTION_COHORT = 20
MIN_METRIC_COHORT = 40
MIN_GLOBAL_COHORT = 50
PRIOR_STRENGTH = 20.0
ONE_SIDED_Z_90 = 1.2815515655446004
MIN_CLOSE_CLV_SAMPLES = 30
MAX_CLOSE_CLV_PENALTY = 0.05


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def weather_bucket_direction(low_f: Any, high_f: Any) -> str:
    if low_f is None:
        return "below"
    if high_f is None:
        return "above"
    return "middle"


def _probability_bin(probability: float) -> int:
    return min(19, max(0, int(float(probability) / PROBABILITY_BIN_WIDTH)))


def _wilson_lower(successes: float, trials: float, z: float = ONE_SIDED_Z_90) -> float:
    if trials <= 0:
        return 0.0
    rate = min(1.0, max(0.0, successes / trials))
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = rate + z2 / (2.0 * trials)
    margin = z * math.sqrt(
        max(0.0, rate * (1.0 - rate) / trials + z2 / (4.0 * trials * trials))
    )
    return min(1.0, max(0.0, (center - margin) / denominator))


@dataclass(frozen=True)
class _WeatherHistoryRow:
    metric: str
    direction: str
    probability_bin: int
    probability: float
    executable_cost: float | None
    correct: bool


@dataclass(frozen=True)
class WeatherCandidateCalibration:
    execution_probability: float
    ready: bool
    allowed: bool
    reason: str | None
    cohort: str
    samples: int
    wins: int
    average_model_probability: float | None
    posterior_lower_probability: float | None
    probability_scale: float
    historical_unit_roi: float | None
    close_clv_samples: int
    average_close_clv: float | None
    close_clv_penalty: float

    def as_metadata(self) -> dict[str, Any]:
        return {
            "execution_probability": self.execution_probability,
            "execution_calibration_ready": self.ready,
            "execution_calibration_allowed": self.allowed,
            "execution_calibration_reason": self.reason,
            "execution_calibration_cohort": self.cohort,
            "execution_calibration_samples": self.samples,
            "execution_calibration_wins": self.wins,
            "execution_calibration_avg_model_prob": self.average_model_probability,
            "execution_calibration_lower_prob": self.posterior_lower_probability,
            "execution_calibration_scale": self.probability_scale,
            "execution_calibration_unit_roi": self.historical_unit_roi,
            "execution_close_clv_samples": self.close_clv_samples,
            "execution_average_close_clv": self.average_close_clv,
            "execution_close_clv_penalty": self.close_clv_penalty,
        }


class WeatherExecutionCalibrator:
    def __init__(
        self,
        rows: list[_WeatherHistoryRow],
        *,
        close_clv_samples: int,
        average_close_clv: float | None,
    ) -> None:
        self.rows = rows
        self.close_clv_samples = int(close_clv_samples)
        self.average_close_clv = average_close_clv

    def _cohort(self, metric: str, direction: str, probability: float) -> tuple[str, list[_WeatherHistoryRow]]:
        probability_bin = _probability_bin(probability)
        exact = [
            row
            for row in self.rows
            if row.metric == metric
            and row.direction == direction
            and row.probability_bin == probability_bin
        ]
        if len(exact) >= MIN_EXACT_COHORT:
            return f"metric_direction_pbin:{metric}:{direction}:{probability_bin}", exact
        directional = [
            row for row in self.rows if row.metric == metric and row.direction == direction
        ]
        if len(directional) >= MIN_DIRECTION_COHORT:
            return f"metric_direction:{metric}:{direction}", directional
        metric_rows = [row for row in self.rows if row.metric == metric]
        if len(metric_rows) >= MIN_METRIC_COHORT:
            return f"metric:{metric}", metric_rows
        if len(self.rows) >= MIN_GLOBAL_COHORT:
            return "global", list(self.rows)
        return "cold_start", []

    def calibrate(
        self,
        *,
        metric: str,
        bucket_low_f: Any,
        bucket_high_f: Any,
        probability: float,
        executable_cost: float,
        min_net_edge: float,
    ) -> WeatherCandidateCalibration:
        probability = min(0.999, max(0.001, float(probability)))
        executable_cost = float(executable_cost)
        direction = weather_bucket_direction(bucket_low_f, bucket_high_f)
        cohort_name, cohort = self._cohort(str(metric), direction, probability)
        clv_penalty = 0.0
        if (
            self.close_clv_samples >= MIN_CLOSE_CLV_SAMPLES
            and self.average_close_clv is not None
        ):
            clv_penalty = min(
                MAX_CLOSE_CLV_PENALTY,
                max(0.0, -float(self.average_close_clv)),
            )
        if not cohort:
            return WeatherCandidateCalibration(
                execution_probability=max(0.001, probability - clv_penalty),
                ready=False,
                allowed=False,
                reason="WEATHER_EXECUTION_CALIBRATION_COLD_START",
                cohort=cohort_name,
                samples=0,
                wins=0,
                average_model_probability=None,
                posterior_lower_probability=None,
                probability_scale=0.0,
                historical_unit_roi=None,
                close_clv_samples=self.close_clv_samples,
                average_close_clv=self.average_close_clv,
                close_clv_penalty=clv_penalty,
            )

        samples = len(cohort)
        wins = sum(1 for row in cohort if row.correct)
        average_probability = sum(row.probability for row in cohort) / samples
        posterior_successes = wins + PRIOR_STRENGTH * average_probability
        posterior_trials = samples + PRIOR_STRENGTH
        lower_probability = _wilson_lower(posterior_successes, posterior_trials)
        scale = (
            min(1.0, max(0.0, lower_probability / average_probability))
            if average_probability > 1e-9
            else 0.0
        )
        calibrated_probability = probability * scale
        execution_probability = max(0.001, calibrated_probability - clv_penalty)

        cost_rows = [row for row in cohort if row.executable_cost is not None]
        historical_unit_roi: float | None = None
        profitable_history = False
        if cost_rows:
            total_cost = sum(float(row.executable_cost) for row in cost_rows)
            unit_pnl = sum(
                (1.0 if row.correct else 0.0) - float(row.executable_cost)
                for row in cost_rows
            )
            if total_cost > 0:
                historical_unit_roi = unit_pnl / total_cost
            average_cost = total_cost / len(cost_rows)
            profitable_history = unit_pnl > 0.0 and lower_probability > average_cost

        calibrated_edge = execution_probability - executable_cost
        allowed = profitable_history and calibrated_edge >= float(min_net_edge)
        if not profitable_history:
            reason = "WEATHER_EXECUTION_COHORT_NOT_PROFITABLE"
        elif calibrated_edge < float(min_net_edge):
            reason = "WEATHER_EXECUTION_CALIBRATED_EDGE_TOO_LOW"
        else:
            reason = None
        return WeatherCandidateCalibration(
            execution_probability=execution_probability,
            ready=True,
            allowed=allowed,
            reason=reason,
            cohort=cohort_name,
            samples=samples,
            wins=wins,
            average_model_probability=average_probability,
            posterior_lower_probability=lower_probability,
            probability_scale=scale,
            historical_unit_roi=historical_unit_roi,
            close_clv_samples=self.close_clv_samples,
            average_close_clv=self.average_close_clv,
            close_clv_penalty=clv_penalty,
        )


def load_weather_execution_calibrator(
    db,
    *,
    history_limit: int = 500,
    clv_limit: int = 200,
) -> WeatherExecutionCalibrator:
    """Load bounded, resolved training history and recent closing-line evidence."""
    prediction_rows: list[dict[str, Any]] = []
    try:
        prediction_rows = (
            db.table("model_predictions")
            .select("prob,is_correct,metadata,resolved_at")
            .eq("source", WEATHER_PREDICTION_SOURCE)
            .not_.is_("resolved_at", "null")
            .eq("metadata->>label_source", "venue_official")
            .eq("metadata->>eligible_before_observation_gate", "true")
            .order("resolved_at", desc=True)
            .limit(max(1, int(history_limit)))
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning("Weather execution calibration history lookup failed: {}", exc)

    history: list[_WeatherHistoryRow] = []
    for row in prediction_rows:
        meta = _metadata(row.get("metadata"))
        try:
            probability = float(row.get("prob"))
        except (TypeError, ValueError):
            continue
        correct = row.get("is_correct")
        metric = str(meta.get("metric") or "")
        if correct is None or metric not in {"high", "low"} or not math.isfinite(probability):
            continue
        cost: float | None
        try:
            cost = float(meta.get("executable_cost"))
            if not math.isfinite(cost) or not 0.0 < cost < 1.0:
                cost = None
        except (TypeError, ValueError):
            cost = None
        history.append(
            _WeatherHistoryRow(
                metric=metric,
                direction=weather_bucket_direction(
                    meta.get("bucket_low_f"), meta.get("bucket_high_f")
                ),
                probability_bin=_probability_bin(probability),
                probability=probability,
                executable_cost=cost,
                correct=correct is True,
            )
        )

    close_clvs: list[float] = []
    try:
        clv_rows = (
            db.table("clv_obligations")
            .select("entry_market_price,entry_price,obs_close_price,metadata")
            .not_.is_("obs_close_price", "null")
            .eq("metadata->>model_version", MODEL_VERSION)
            .order("obs_close_ts", desc=True)
            .limit(max(1, int(clv_limit)))
            .execute()
            .data
            or []
        )
        for row in clv_rows:
            entry = row.get("entry_market_price")
            if entry is None:
                entry = row.get("entry_price")
            try:
                value = float(row.get("obs_close_price")) - float(entry)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                close_clvs.append(value)
    except Exception as exc:
        logger.warning("Weather closing-line calibration lookup failed: {}", exc)

    average_clv = sum(close_clvs) / len(close_clvs) if close_clvs else None
    return WeatherExecutionCalibrator(
        history,
        close_clv_samples=len(close_clvs),
        average_close_clv=average_clv,
    )
