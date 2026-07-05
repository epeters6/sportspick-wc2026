import os
import sys
import logging

logger = logging.getLogger(__name__)

# Dynamically add pavlov to path so we can import the Quant models
pavlov_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "pavlov", "pavlov-mlb-bot")
if pavlov_path not in sys.path:
    sys.path.insert(0, pavlov_path)

try:
    from pipeline.mlb_client import get_todays_games
    from pipeline.mlb_signal_engine import calculate_win_probability
    from backend.trading.market_matcher import _canonical
except ImportError as e:
    logger.error(f"Failed to import MLB quant engine: {e}")
    calculate_win_probability = None

def get_mlb_quant_probability(home_team: str, away_team: str) -> dict | None:
    """
    Fetches the day's MLB schedule and runs the Quant Model (Weather + Pitcher stats)
    on the specific match to generate a win probability.
    """
    if not calculate_win_probability:
        return None
        
    try:
        games = get_todays_games()
    except Exception as exc:
        logger.error(f"MLB Quant failed to fetch schedule: {exc}")
        return None
        
    home_canon = _canonical(home_team) or home_team
    away_canon = _canonical(away_team) or away_team
    
    for g in games:
        g_home = _canonical(g["home"]["name"]) or g["home"]["name"]
        g_away = _canonical(g["away"]["name"]) or g["away"]["name"]
        
        if (g_home == home_canon and g_away == away_canon) or (g_home == home_team and g_away == away_team):
            # Run the quant model
            try:
                res = calculate_win_probability(g, 1000.0) # Bankroll is arbitrary here
                if res and "home_prob" in res and "away_prob" in res:
                    return {
                        "home_prob": res["home_prob"],
                        "away_prob": res["away_prob"]
                    }
            except Exception as exc:
                logger.error(f"Quant model execution failed for {home_team} vs {away_team}: {exc}")
                return None
                
    return None
