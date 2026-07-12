from typing import Optional
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.sports_probability_model import predict_sports_probability, SportsPrediction

SUPPORTED_SOCCER_MARKETS = {
    "REGULATION_WIN",
    "ADVANCE",
    "DRAW_NO_BET",
    "GROUP_WINNER",
    "TO_WIN_TOURNAMENT",
    "MONEYLINE",
    "TOTAL_GOALS",
    "BTTS"
}

def build_soccer_features(
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
    market_type: str,
    resolution_rule: str,
    attack_strength_diff: Optional[float],
    defense_strength_diff: Optional[float],
    rest_days_diff: Optional[float],
    injury_absence_score: Optional[float],
    venue_or_home_advantage: Optional[float]
) -> SportsEventFeatures:

    if not market_type:
        raise ValueError("MISSING_SOCCER_MARKET_TYPE")
    if not resolution_rule:
        raise ValueError("MISSING_SOCCER_RESOLUTION_RULE")
        
    mt = market_type.upper()
    if mt not in SUPPORTED_SOCCER_MARKETS:
        raise ValueError(f"UNSUPPORTED_SOCCER_MARKET_TYPE: {market_type}")

    sport_specific = {
        "market_type": mt,
        "resolution_rule": resolution_rule,
        "attack_strength_diff": attack_strength_diff if attack_strength_diff is not None else "MISSING",
        "defense_strength_diff": defense_strength_diff if defense_strength_diff is not None else "MISSING",
        "rest_days_diff": rest_days_diff if rest_days_diff is not None else "MISSING",
        "injury_absence_score": injury_absence_score if injury_absence_score is not None else "MISSING",
        "venue_or_home_advantage": venue_or_home_advantage if venue_or_home_advantage is not None else "MISSING",
    }

    return SportsEventFeatures(
        sport="SOCCER",
        league="SOCCER",
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

def predict_soccer_3way_probabilities(features: SportsEventFeatures) -> tuple[float, float, float]:
    """Returns (P_home, P_draw, P_away)"""
    pred_home = predict_sports_probability(features)
    if pred_home.rejection_reason:
        raise ValueError(pred_home.rejection_reason)
        
    p_home = pred_home.model_prob
    p_draw = 0.25 # baseline heuristic draw probability for tests
    
    if p_home + p_draw > 1.0:
        p_home = 1.0 - p_draw
        
    p_away = max(0.0, 1.0 - p_home - p_draw)
    
    return p_home, p_draw, p_away

def predict_soccer_binary_contract(features: SportsEventFeatures, side: str = "home") -> SportsPrediction:
    if features.sport_specific["market_type"] in ("REGULATION_WIN", "MONEYLINE"):
        try:
            p_home, p_draw, p_away = predict_soccer_3way_probabilities(features)
            
            p_target = p_home if side == "home" else p_away
            
            # Map back to SportsPrediction object with correct target prob
            pred = predict_sports_probability(features)
            pred.model_prob = p_target
            pred.edge_before_execution = p_target - pred.market_prob
            return pred
        except ValueError as e:
            pred = predict_sports_probability(features)
            pred.rejection_reason = str(e)
            return pred
            
    # For ADVANCE, GROUP_WINNER, etc. it's already a binary probability space
    return predict_sports_probability(features)
