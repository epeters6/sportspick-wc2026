from typing import List, Tuple
from loguru import logger
from pavlov.pipeline.settlement_resolver import NormalizedWeatherEvent

def mask_impossible_buckets(
    events: List[NormalizedWeatherEvent],
    p_vector: List[float],
    high_so_far: float
) -> List[float]:
    """
    Zero out probabilities for buckets that are already impossible because the 
    observed high_so_far has exceeded their ceiling.
    """
    if len(events) != len(p_vector):
        raise ValueError("Mismatch between events and probability vector length.")
        
    p_constrained = list(p_vector)
    
    for i, event in enumerate(events):
        if event.bucket_high_f < high_so_far:
            p_constrained[i] = 0.0
            
    total = sum(p_constrained)
    if total <= 0:
        raise ValueError(f"IMPOSSIBLE_BUCKET_ONLY: Nowcast constraint eliminated ALL probabilities (high_so_far: {high_so_far})")
        
    return [p / total for p in p_constrained]
