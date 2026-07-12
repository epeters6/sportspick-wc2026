from typing import List, Tuple
import math
from loguru import logger
from pavlov.pipeline.settlement_resolver import NormalizedWeatherEvent

# Initial conservative defaults per the implementation plan
SIGMA_HISTORICAL_BY_LEAD = {
    "same_day_after_noon": 0.9,
    "same_day_morning": 1.2,
    "day_ahead": 1.7,
    "two_day": 2.2,
    "three_to_five_day": 3.0,
}

def get_historical_sigma(lead_days: int, hour: int = 0) -> float:
    if lead_days <= 0:
        if hour >= 12: return SIGMA_HISTORICAL_BY_LEAD["same_day_after_noon"]
        return SIGMA_HISTORICAL_BY_LEAD["same_day_morning"]
    elif lead_days == 1:
        return SIGMA_HISTORICAL_BY_LEAD["day_ahead"]
    elif lead_days == 2:
        return SIGMA_HISTORICAL_BY_LEAD["two_day"]
    else:
        return SIGMA_HISTORICAL_BY_LEAD["three_to_five_day"]

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
    bias_correction: float = 0.0
) -> Tuple[List[NormalizedWeatherEvent], List[float]]:
    """
    Given a list of mutually exclusive weather events (buckets) for a single station/date,
    and the raw ensemble stats, calculate the calibrated mu/sigma and generate a normalized
    probability vector.
    """
    if not events:
        return [], []
        
    # 1. Estimate true forecast uncertainty
    sigma_historical = get_historical_sigma(lead_days, hour)
    sigma_station_resolution = 0.30
    sigma_nowcast_regime = 0.0
    
    sigma_final_squared = (
        (ensemble_sigma ** 2)
        + (sigma_historical ** 2)
        + (sigma_station_resolution ** 2)
        + (sigma_nowcast_regime ** 2)
    )
    sigma_final = math.sqrt(sigma_final_squared)
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
