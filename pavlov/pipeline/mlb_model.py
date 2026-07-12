from typing import Optional
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.sports_probability_model import predict_sports_probability, SportsPrediction

def build_mlb_features(
    event_id: str,
    market_id: str,
    team_a: str,
    team_b: str,
    start_time,
    snapshot_time,
    market_prob_baseline: float,
    market_price_source: str,
    elo_team_a: Optional[float],
    elo_team_b: Optional[float],
    elo_diff: Optional[float],
    consensus_pick_count_a: int,
    consensus_pick_count_b: int,
    consensus_weighted_signal: float,
    source_clv_weighted_signal: float,
    source_count: int,
    independent_source_count: int,
    starting_pitcher_team_a: Optional[str],
    starting_pitcher_team_b: Optional[str],
    pitcher_rating_diff: Optional[float],
    bullpen_fatigue_diff: Optional[float],
    lineup_confirmed: Optional[bool],
    park_factor: Optional[float],
    weather_run_environment: Optional[float],
    travel_rest_diff: Optional[float]
) -> SportsEventFeatures:

    sport_specific = {
        "starting_pitcher_team_a": starting_pitcher_team_a if starting_pitcher_team_a is not None else "MISSING",
        "starting_pitcher_team_b": starting_pitcher_team_b if starting_pitcher_team_b is not None else "MISSING",
        "pitcher_rating_diff": pitcher_rating_diff if pitcher_rating_diff is not None else "MISSING",
        "bullpen_fatigue_diff": bullpen_fatigue_diff if bullpen_fatigue_diff is not None else "MISSING",
        "lineup_confirmed": lineup_confirmed if lineup_confirmed is not None else "MISSING",
        "park_factor": park_factor if park_factor is not None else "MISSING",
        "weather_run_environment": weather_run_environment if weather_run_environment is not None else "MISSING",
        "travel_rest_diff": travel_rest_diff if travel_rest_diff is not None else "MISSING"
    }

    return SportsEventFeatures(
        sport="MLB",
        league="MLB",
        event_id=event_id,
        market_id=market_id,
        team_a=team_a,
        team_b=team_b,
        start_time=start_time,
        snapshot_time=snapshot_time,
        market_prob_baseline=market_prob_baseline,
        market_price_source=market_price_source,
        elo_team_a=elo_team_a,
        elo_team_b=elo_team_b,
        elo_diff=elo_diff,
        consensus_pick_count_a=consensus_pick_count_a,
        consensus_pick_count_b=consensus_pick_count_b,
        consensus_weighted_signal=consensus_weighted_signal,
        source_clv_weighted_signal=source_clv_weighted_signal,
        source_count=source_count,
        independent_source_count=independent_source_count,
        sport_specific=sport_specific
    )

def predict_mlb_probability(features: SportsEventFeatures) -> SportsPrediction:
    # Overrides generic sports model by incorporating MLB-specific features if they exist
    pred = predict_sports_probability(features)
    
    if pred.rejection_reason:
        return pred
        
    import math
    from pavlov.pipeline.sports_probability_model import logit, inv_logit
    
    logit_p = logit(pred.model_prob)
    
    # MLB-specific logic
    sp = features.sport_specific
    if sp["pitcher_rating_diff"] != "MISSING":
        logit_p += 0.005 * sp["pitcher_rating_diff"]
        
    if sp["bullpen_fatigue_diff"] != "MISSING":
        logit_p += 0.002 * sp["bullpen_fatigue_diff"]
        
    if sp["travel_rest_diff"] != "MISSING":
        logit_p += 0.01 * sp["travel_rest_diff"]
        
    model_prob = inv_logit(logit_p)
    
    return SportsPrediction(
        model_prob=model_prob,
        market_prob=pred.market_prob,
        edge_before_execution=model_prob - pred.market_prob,
        feature_snapshot=features.__dict__,
        model_version="mlb_v1",
        model_type="logit_linear",
        coefficient_source="default_config",
        calibration_status="uncalibrated_shadow",
        rejection_reason=None
    )
