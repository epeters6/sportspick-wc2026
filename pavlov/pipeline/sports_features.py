from dataclasses import dataclass
from typing import Literal, Optional
from datetime import datetime

@dataclass
class SportsEventFeatures:
    sport: Literal["MLB", "SOCCER"]
    league: str
    event_id: str
    market_id: str
    team_a: str
    team_b: str
    start_time: datetime
    snapshot_time: datetime

    market_prob_baseline: float
    market_price_source: str

    elo_team_a: Optional[float]
    elo_team_b: Optional[float]
    elo_diff: Optional[float]

    consensus_pick_count_a: int
    consensus_pick_count_b: int
    consensus_weighted_signal: float

    source_clv_weighted_signal: float
    source_count: int
    independent_source_count: int

    sport_specific: dict

    def validate(self):
        if self.market_prob_baseline is None:
            raise ValueError("MISSING_MARKET_BASELINE")
        if not (0.01 <= self.market_prob_baseline <= 0.99):
            raise ValueError("MARKET_BASELINE_OUT_OF_BOUNDS")
        if not self.market_price_source:
            raise ValueError("MISSING_MARKET_PRICE_SOURCE")
            
        if not self.team_a or not self.team_b:
            raise ValueError("MISSING_TEAMS")
        if not self.event_id or not self.market_id:
            raise ValueError("MISSING_IDS")
            
        if self.start_time is None:
            raise ValueError("MISSING_START_TIME")
        if self.snapshot_time is None:
            raise ValueError("MISSING_SNAPSHOT_TIME")
            
        if self.snapshot_time >= self.start_time:
            raise ValueError("SNAPSHOT_AFTER_EVENT_START")
            
        if self.elo_team_a is None and self.elo_team_b is None and self.elo_diff is None:
            raise ValueError("MISSING_CRITICAL_RATING")
            
        self.validate_point_in_time_features()

    def validate_point_in_time_features(self):
        # Prevent leakage of post-event data
        leakage_keys = {
            "final_score", "result", "settlement", "closing_price", 
            "closing_line", "pnl", "actual_win", "postgame"
        }
        for key in self.sport_specific:
            if any(leak in key.lower() for leak in leakage_keys):
                raise ValueError(f"LEAKAGE_DETECTED_IN_FEATURES: {key}")
