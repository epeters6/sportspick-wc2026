from dataclasses import dataclass
from typing import Literal, Optional
from datetime import datetime

@dataclass
class TradeCandidate:
    strategy: str
    platform: str
    market_id: str
    outcome_id: str
    event_id: str
    side: Literal["YES", "NO"]
    model_prob: float
    market_prob: Optional[float]
    executable_cost: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    visible_depth: float
    fee_per_share: float
    slippage_buffer: float
    max_shares_by_depth: float
    max_shares_by_risk: float
    bankroll: float
    event_exposure_cap: float
    bucket_or_outcome_exposure_cap: float
    timestamp: datetime
    metadata: dict
    received_timestamp: Optional[datetime] = None
    orderbook_timestamp: Optional[datetime] = None

@dataclass
class SizedOrder:
    candidate: TradeCandidate
    target_shares: float
    target_cost: float
    limit_price: float
    expected_log_growth_delta: float
    rejection_reason: Optional[str] = None
