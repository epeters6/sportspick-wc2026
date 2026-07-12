from dataclasses import dataclass

@dataclass
class RiskCaps:
    max_event_exposure_pct: float
    max_outcome_exposure_pct: float
    max_strategy_exposure_pct: float
    max_platform_exposure_pct: float
    max_daily_loss_pct: float
    max_weekly_loss_pct: float
    min_net_edge: float
    min_log_growth_delta: float

    def get_event_exposure_cap_dollars(self, bankroll: float) -> float:
        return self.max_event_exposure_pct * bankroll

    def get_outcome_exposure_cap_dollars(self, bankroll: float) -> float:
        return self.max_outcome_exposure_pct * bankroll

    def get_strategy_exposure_cap_dollars(self, bankroll: float) -> float:
        return self.max_strategy_exposure_pct * bankroll

    def get_platform_exposure_cap_dollars(self, bankroll: float) -> float:
        return self.max_platform_exposure_pct * bankroll
