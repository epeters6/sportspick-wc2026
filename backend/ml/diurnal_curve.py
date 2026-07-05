"""
diurnal_curve.py
Models the fraction of a day's expected temperature warming that has already
occurred (vs. still ahead) at a given hour, so the intraday nowcast can decide
how much to trust an observed deviation from the morning forecast.

WHY: A naive nowcast that shifts the forecasted daily high by whatever the
current observed anomaly is will overreact to transient noise (a passing
cloud, a brief wind shift) -- especially early in the day, when there's still
a lot of time for the temperature trajectory to change. The same-size anomaly
observed near the historically-expected time of the daily max is much more
likely to persist into the final reading, since there's little time left for
anything to change it.

TWO MODELS PROVIDED, IN ORDER OF PREFERENCE:

1. elapsed_warming_fraction_from_climatology() -- USE THIS if you have (or can
   build) per-station, per-month hourly climatology from your own historical
   observations. Real diurnal curves are rarely perfectly symmetric (morning
   warming is often faster than the final approach to the afternoon peak,
   since the rate of warming tracks net radiation surplus, not just elapsed
   time) -- this uses the ACTUAL shape from your data instead of assuming one.
   Your MOS verification database (logging station observations for bias
   correction) gives you most of the raw material for this almost for free.

2. elapsed_warming_fraction() -- a raised-cosine (half-sine) fallback for
   when you don't have that climatology yet. Smooth, zero-slope at both
   endpoints (matching the physical turning points of a real curve), which
   is at least a better default than a linear ramp -- but it assumes a
   SYMMETRIC rise, which is a simplification, not a verified fact about any
   particular station. Treat it as a placeholder to replace with (1) once
   you've logged enough station-hours.

    elapsed_fraction(t) = 0.5 * (1 - cos(pi * (t - t_min) / (t_max - t_min)))
"""

import math
from typing import List, Optional, Tuple


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def elapsed_warming_fraction(current_hour: float, t_min: float = 6.0, t_max: float = 15.0) -> float:
    """
    current_hour, t_min, t_max: hour of day as a float in the station's LOCAL
    STANDARD time (e.g. 14.5 = 2:30pm), all on a 24-hour same-day basis.

    Returns a value in [0, 1]: how much of today's expected min-to-max
    warming has already happened by current_hour, under a symmetric
    raised-cosine assumption. See module docstring for when to use this vs.
    the climatology-based version.

    NOTE: t_min/t_max default to a mid-latitude, non-winter placeholder.
    Don't rely on these defaults for real trading -- pass in values derived
    from your forecast model's own predicted argmin/argmax hour, or from
    station climatology for that month.
    """
    if t_max <= t_min:
        raise ValueError("t_max must be after t_min")
    if not (0 <= current_hour <= 48):
        raise ValueError("current_hour looks out of range -- expected a same-day hour in [0, 24) "
                         "(a little slack allowed for callers passing 24-27 for post-midnight)")

    if current_hour <= t_min:
        return 0.0
    if current_hour >= t_max:
        return 1.0

    progress = (current_hour - t_min) / (t_max - t_min)
    return 0.5 * (1 - math.cos(math.pi * progress))


def remaining_warming_fraction(current_hour: float, t_min: float = 6.0, t_max: float = 15.0) -> float:
    return 1.0 - elapsed_warming_fraction(current_hour, t_min, t_max)


def _interpolate_hourly(values: List[float], hour: float) -> float:
    """Linear interpolation of a fractional hour within a 24-length hourly
    climatology array (index 0 = midnight ... index 23 = 11pm), wrapping
    around midnight."""
    lo = int(math.floor(hour)) % 24
    hi = (lo + 1) % 24
    frac = hour - math.floor(hour)
    return values[lo] * (1 - frac) + values[hi] * frac


def elapsed_warming_fraction_from_climatology(current_hour: float, hourly_climatology: List[float]) -> float:
    """
    hourly_climatology: exactly 24 values (index 0 = hour 0/midnight ... index
    23 = hour 23), the station's typical/climatological temperature at each
    hour for this time of year, built from your own historical station data.

    This reflects the ACTUAL shape of the local diurnal cycle -- which is
    rarely perfectly symmetric -- rather than assuming the idealized
    raised-cosine curve above. Prefer this once you have enough station-hours
    logged to build it.

    Returns a value in [0, 1], same semantics as elapsed_warming_fraction().
    """
    if len(hourly_climatology) != 24:
        raise ValueError("hourly_climatology must have exactly 24 values, one per hour of day")

    current_hour = current_hour % 24  # tolerate post-midnight callers passing e.g. 25.0

    t_min_hour = hourly_climatology.index(min(hourly_climatology))
    t_max_hour = hourly_climatology.index(max(hourly_climatology))

    # Typical case: trough in early morning, peak in afternoon, same calendar day.
    # If your climatology is unusual enough to violate this (e.g. a station
    # with a genuinely atypical diurnal pattern), this simple version isn't
    # the right tool -- that's a signal to fall back to the parametric curve.
    if t_min_hour >= t_max_hour:
        raise ValueError(
            f"Expected the climatological trough (hour {t_min_hour}) to come before the "
            f"peak (hour {t_max_hour}) on the same day -- this climatology doesn't fit the "
            "simple same-day model this function assumes."
        )

    if current_hour <= t_min_hour:
        return 0.0
    if current_hour >= t_max_hour:
        return 1.0

    total_range = hourly_climatology[t_max_hour] - hourly_climatology[t_min_hour]
    if total_range <= 0:
        return 0.5  # degenerate climatology (flat day) -- no informative shape, use the midpoint

    current_value = _interpolate_hourly(hourly_climatology, current_hour)
    return _clip01((current_value - hourly_climatology[t_min_hour]) / total_range)


def nowcast_adjustment(
    observed_temp: float,
    forecast_temp_at_this_hour: float,
    current_hour: float,
    forecast_spread: float,
    t_min: float = 6.0,
    t_max: float = 15.0,
    max_spread_tightening: float = 0.6,
    min_spread_floor: float = 1.0,
    hourly_climatology: Optional[List[float]] = None,
) -> Tuple[float, float]:
    """
    Combines the diurnal curve with the observed-vs-forecast deviation to
    produce an adjusted mean-shift and tightened spread for the day's high.
    Uses the climatology-based curve if hourly_climatology is supplied,
    otherwise falls back to the symmetric raised-cosine model.

    - The observed deviation is discounted heavily early in the day (low
      confidence it persists to the final high) and trusted almost fully once
      at/after the historical time of the daily max (little time left for
      anything to change).
    - Spread tightens as the day progresses, but never below min_spread_floor
      -- per the earlier guidance that forecast spread should never be
      allowed to collapse to false certainty, even late in the day.

    Returns (mean_shift, adjusted_spread). Apply mean_shift additively to your
    model's originally forecast daily high, and use adjusted_spread in place
    of the original spread for the rest of the day's probability calculation.
    """
    if hourly_climatology is not None:
        confidence = elapsed_warming_fraction_from_climatology(current_hour, hourly_climatology)
    else:
        confidence = elapsed_warming_fraction(current_hour, t_min, t_max)

    raw_deviation = observed_temp - forecast_temp_at_this_hour
    mean_shift = raw_deviation * confidence

    tightening = 1 - (max_spread_tightening * confidence)
    adjusted_spread = max(forecast_spread * tightening, min_spread_floor)

    return mean_shift, adjusted_spread


if __name__ == "__main__":
    print("-- symmetric raised-cosine model --")
    for hour in [5, 7, 10, 12, 14, 15, 17]:
        shift, spread = nowcast_adjustment(
            observed_temp=93.0, forecast_temp_at_this_hour=90.0,
            current_hour=hour, forecast_spread=3.0,
        )
        print(f"hour={hour:>2}  mean_shift={shift:+.2f}F  adjusted_spread={spread:.2f}F")

    print("\n-- climatology-based model (front-loaded warming example) --")
    # A synthetic climatology where most of the day's warming happens by
    # midday and the afternoon is a slow plateau -- deliberately asymmetric,
    # to show this diverges from the symmetric assumption above.
    climo = [60, 59, 58, 58, 57, 58, 60, 64, 69, 74, 78, 81,
             83, 84, 84.5, 85, 85, 84.5, 83, 80, 76, 72, 68, 64]
    for hour in [5, 7, 10, 12, 14, 15, 17]:
        shift, spread = nowcast_adjustment(
            observed_temp=93.0, forecast_temp_at_this_hour=90.0,
            current_hour=hour, forecast_spread=3.0, hourly_climatology=climo,
        )
        print(f"hour={hour:>2}  mean_shift={shift:+.2f}F  adjusted_spread={spread:.2f}F")
