import logging
import math
import os
import sys
from dataclasses import dataclass

# Ensure backend paths are loaded
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.db import get_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeatherCalibration:
    bias_correction: float
    residual_sigma: float
    station_samples: int
    pooled_samples: int
    source: str


class WeatherMOS:
    """Hierarchically calibrated Model Output Statistics for weather."""

    def __init__(self):
        self.db = get_db()
        self._cache: dict[tuple[str, str, int, str], WeatherCalibration] = {}

    @staticmethod
    def _errors(rows: list[dict], metric: str) -> list[float]:
        predicted_col = "predicted_high" if metric == "high" else "predicted_low"
        actual_col = "actual_high" if metric == "high" else "actual_low"
        errors: list[float] = []
        for row in rows:
            predicted = row.get(predicted_col)
            actual = row.get(actual_col)
            if predicted is None or actual is None:
                continue
            try:
                error = float(actual) - float(predicted)
            except (TypeError, ValueError):
                continue
            if math.isfinite(error):
                errors.append(error)
        return errors

    def calculate_calibration(
        self,
        station_id: str,
        model_name: str,
        lead_time_days: int,
        metric: str = "high",
    ) -> WeatherCalibration:
        """Return leakage-safe bias and total residual sigma.

        Only rows with observed actuals can enter the residual sample, so a
        future target day cannot train its own forecast. Station bias is shrunk
        toward the metric/lead pool, while sigma may widen but never narrow the
        conservative v2 floor.
        """
        metric = "low" if str(metric).lower() == "low" else "high"
        lead = max(int(lead_time_days), 0)
        cache_key = (station_id, model_name, lead, metric)
        if cache_key in self._cache:
            return self._cache[cache_key]

        from pavlov.pipeline.probability_model import get_historical_sigma

        floor_sigma = get_historical_sigma(lead, 0, metric)
        try:
            pooled_rows = (
                self.db.table("weather_verification")
                .select(
                    "station_id,target_date,predicted_high,predicted_low,actual_high,actual_low"
                )
                .eq("model_name", model_name)
                .eq("lead_time_days", lead)
                .order("target_date", desc=True)
                .limit(250)
                .execute()
                .data
                or []
            )
        except Exception as exc:
            logger.warning("MOS pooled verification query failed: %s", exc)
            result = WeatherCalibration(0.0, floor_sigma, 0, 0, "v2_floor")
            self._cache[cache_key] = result
            return result

        pooled_errors = self._errors(pooled_rows, metric)
        station_errors = self._errors(
            [row for row in pooled_rows if row.get("station_id") == station_id],
            metric,
        )[:30]

        pooled_bias = (
            sum(pooled_errors) / len(pooled_errors) if len(pooled_errors) >= 5 else 0.0
        )
        if len(station_errors) >= 3:
            station_bias = sum(station_errors) / len(station_errors)
            station_weight = len(station_errors) / (len(station_errors) + 12.0)
            bias = station_weight * station_bias + (1.0 - station_weight) * pooled_bias
            source = "pooled_station_v2"
        else:
            bias = pooled_bias
            source = "pooled_metric_lead_v2" if pooled_errors else "v2_floor"
        bias = max(-5.0, min(5.0, bias))

        if len(pooled_errors) >= 8:
            residual_sigma = math.sqrt(
                sum((error - pooled_bias) ** 2 for error in pooled_errors)
                / len(pooled_errors)
            )
            if not math.isfinite(residual_sigma):
                residual_sigma = floor_sigma
        else:
            residual_sigma = floor_sigma
        residual_sigma = max(floor_sigma, min(10.0, residual_sigma))

        result = WeatherCalibration(
            bias_correction=bias,
            residual_sigma=residual_sigma,
            station_samples=len(station_errors),
            pooled_samples=len(pooled_errors),
            source=source,
        )
        self._cache[cache_key] = result
        logger.info(
            "MOS v2: %s %s lead=%s bias=%+.2fF sigma=%.2fF n_station=%s n_pool=%s",
            station_id,
            metric,
            lead,
            bias,
            residual_sigma,
            len(station_errors),
            len(pooled_errors),
        )
        return result

    def calculate_bias(
        self,
        station_id: str,
        model_name: str,
        lead_time_days: int,
        metric: str = "high",
    ) -> float:
        """Backward-compatible bias-only interface."""
        return self.calculate_calibration(
            station_id, model_name, lead_time_days, metric
        ).bias_correction


# Singleton instance
mos_engine = WeatherMOS()
