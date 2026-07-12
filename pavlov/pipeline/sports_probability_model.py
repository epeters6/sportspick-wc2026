import math
from dataclasses import dataclass
from typing import Optional
from pavlov.pipeline.sports_features import SportsEventFeatures

@dataclass
class SportsPrediction:
    model_prob: float
    market_prob: float
    edge_before_execution: float
    feature_snapshot: dict
    model_version: str
    model_type: str
    coefficient_source: str
    calibration_status: str
    rejection_reason: Optional[str]

def logit(p: float) -> float:
    p = max(0.0001, min(0.9999, p))
    return math.log(p / (1.0 - p))

def inv_logit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def predict_sports_probability(features: SportsEventFeatures) -> SportsPrediction:
    """
    Logit-linear probability model with default coefficients.
    """
    try:
        features.validate()
    except ValueError as e:
        return SportsPrediction(
            model_prob=0.0,
            market_prob=features.market_prob_baseline if features.market_prob_baseline else 0.0,
            edge_before_execution=0.0,
            feature_snapshot=features.__dict__,
            model_version="sports_v1",
            model_type="logit_linear",
            coefficient_source="default_config",
            calibration_status="uncalibrated_shadow",
            rejection_reason=str(e)
        )
        
    # Baseline
    market_p = features.market_prob_baseline
    
    # Feature weights (default heuristics mapped to logistic scale for now)
    beta_0 = 0.0
    beta_market = 1.0
    beta_elo = 0.001
    beta_consensus = 0.05
    beta_source_quality = 0.1
    
    elo_diff = features.elo_diff or 0.0
    
    logit_p_model = (
        beta_0
        + beta_market * logit(market_p)
        + beta_elo * elo_diff
        + beta_consensus * features.consensus_weighted_signal
        + beta_source_quality * features.source_clv_weighted_signal
    )
    
    model_prob = inv_logit(logit_p_model)
    
    return SportsPrediction(
        model_prob=model_prob,
        market_prob=market_p,
        edge_before_execution=model_prob - market_p,
        feature_snapshot=features.__dict__,
        model_version="sports_v1",
        model_type="logit_linear",
        coefficient_source="default_config",
        calibration_status="uncalibrated_shadow",
        rejection_reason=None
    )
