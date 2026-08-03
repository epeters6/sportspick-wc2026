from typing import List, Tuple
import math
from loguru import logger
from pavlov.pipeline.settlement_resolver import NormalizedWeatherEvent

# Total forecast-error sigma floors (degrees F), calibrated from verified station
# residuals.  These are deliberately metric-specific: daily highs have been much
# noisier than lows, especially one day ahead.  They represent total residual
# uncertainty, so they must not be added in quadrature to ensemble spread.
SIGMA_TOTAL_BY_METRIC_AND_LEAD = {
    "high": {
        "same_day_after_noon": 4.25,
        "same_day_morning": 4.50,
        "day_ahead": 5.25,
        "two_day": 5.75,
        "three_to_five_day": 6.50,
    },
    "low": {
        "same_day_after_noon": 2.25,
        "same_day_morning": 2.50,
        "day_ahead": 3.25,
        "two_day": 3.75,
        "three_to_five_day": 4.50,
    },
}

# Backward-compatible name for callers/tests that inspected the old constant.
SIGMA_HISTORICAL_BY_LEAD = SIGMA_TOTAL_BY_METRIC_AND_LEAD["high"]


def get_historical_sigma(
    lead_days: int, hour: int = 0, metric: str = "high"
) -> float:
    metric_sigmas = SIGMA_TOTAL_BY_METRIC_AND_LEAD.get(
        str(metric).lower(), SIGMA_TOTAL_BY_METRIC_AND_LEAD["high"]
    )
    if lead_days <= 0:
        key = "same_day_after_noon" if hour >= 12 else "same_day_morning"
    elif lead_days == 1:
        key = "day_ahead"
    elif lead_days == 2:
        key = "two_day"
    else:
        key = "three_to_five_day"
    return metric_sigmas[key]

def validate_probability_vector(name: str, p: List[float], tol: float = 1e-6):
    if any(math.isnan(x) or math.isinf(x) for x in p):
        raise ValueError(f"{name} contains non-finite values")
    if any(x < -tol for x in p):
        raise ValueError(f"{name} contains negative probabilities")
    total = sum(p)
    if abs(total - 1.0) > tol:
        raise ValueError(f"{name} must sum to 1.0, got {total}")

def normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard normal CDF."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / sigma
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def calculate_bucket_probability(lo_f: float, hi_f: float, mu: float, sigma: float) -> float:
    """Calculate the probability that the normal distribution falls in [lo_f, hi_f)."""
    p_hi = normal_cdf(hi_f, mu, sigma)
    p_lo = normal_cdf(lo_f, mu, sigma)
    return max(0.0, p_hi - p_lo)

def generate_event_probability_vector(
    events: List[NormalizedWeatherEvent],
    ensemble_mu: float,
    ensemble_sigma: float,
    lead_days: int,
    hour: int = 0,
    bias_correction: float = 0.0,
    empirical_sigma: float | None = None,
) -> Tuple[List[NormalizedWeatherEvent], List[float]]:
    """
    Given a list of mutually exclusive weather events (buckets) for a single station/date,
    and the raw ensemble stats, calculate the calibrated mu/sigma and generate a normalized
    probability vector.
    """
    if not events:
        return [], []
        
    # 1. Estimate true forecast uncertainty.  Empirical/MOS sigma is a total
    # residual estimate; ensemble spread is only a lower-bound signal and is not
    # added to it (doing so double-counts the same forecast disagreement).
    metric = getattr(events[0], "metric", "high")
    sigma_historical = get_historical_sigma(lead_days, hour, metric)
    if empirical_sigma is not None:
        try:
            if math.isfinite(float(empirical_sigma)) and float(empirical_sigma) > 0:
                sigma_historical = max(sigma_historical, float(empirical_sigma))
        except (TypeError, ValueError):
            pass
    sigma_station_resolution = 0.30
    ensemble_floor = math.sqrt(max(0.0, ensemble_sigma) ** 2 + sigma_station_resolution ** 2)
    sigma_final = max(sigma_historical, ensemble_floor)
    sigma_final = max(sigma_final, 1.5)  # Enforce sigma floor
    
    # 2. Add station-level residual correction (MOS bias from verification history)
    mu_corrected = ensemble_mu + bias_correction
    
    logger.debug(f"Event Prob Vector: Raw Mu={ensemble_mu:.2f}, Raw Sig={ensemble_sigma:.2f} -> "
                 f"Corr Mu={mu_corrected:.2f}, Final Sig={sigma_final:.2f}")

    # 3. Generate raw probabilities
    P_model_raw = []
    for event in events:
        p = calculate_bucket_probability(event.bucket_low_f, event.bucket_high_f, mu_corrected, sigma_final)
        P_model_raw.append(p)
        
    total_raw = sum(P_model_raw)
    
    # 4. Check for exhaustive bucket space
    # The lowest bucket must be explicitly open-ended (-inf) and highest open-ended (+inf)
    min_lo = min(e.bucket_low_f for e in events)
    max_hi = max(e.bucket_high_f for e in events)
    
    # Also verify no gaps by comparing total_raw to the integral over [min_lo, max_hi]
    expected_integral = calculate_bucket_probability(min_lo, max_hi, mu_corrected, sigma_final)
    
    if min_lo != float("-inf") or max_hi != float("inf") or abs(total_raw - expected_integral) > 1e-4:
        raise ValueError(f"INCOMPLETE_BUCKET_SPACE: Bounds [{min_lo}, {max_hi}] are not exhaustive or have gaps.")

    P_model = [p / total_raw for p in P_model_raw]
        
    # Verify
    validate_probability_vector("P_model", P_model)
    
    return events, P_model
