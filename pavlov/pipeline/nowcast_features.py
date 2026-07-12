from typing import List
from loguru import logger
from pavlov.pipeline.settlement_resolver import NormalizedWeatherEvent

def mask_impossible_buckets(
    events: List[NormalizedWeatherEvent],
    p_vector: List[float],
    observed_extreme: float,
    metric: str = "high",
) -> List[float]:
    """
    Zero out probabilities for buckets that are already impossible given today's
    observed running extreme.

    For HIGH markets: the daily high can only go up, so any bucket whose ceiling
    is below high_so_far is impossible.
    For LOW markets: the daily low can only go down, so any bucket whose floor
    is above low_so_far is impossible.
    """
    if len(events) != len(p_vector):
        raise ValueError("Mismatch between events and probability vector length.")

    p_constrained = list(p_vector)

    for i, event in enumerate(events):
        if metric == "low":
            if event.bucket_low_f > observed_extreme:
                p_constrained[i] = 0.0
        else:
            if event.bucket_high_f < observed_extreme:
                p_constrained[i] = 0.0

    total = sum(p_constrained)
    if total <= 0:
        raise ValueError(f"IMPOSSIBLE_BUCKET_ONLY: Nowcast constraint eliminated ALL probabilities (observed {metric}: {observed_extreme})")

    return [p / total for p in p_constrained]
