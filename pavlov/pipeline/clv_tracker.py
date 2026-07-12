from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import json
import os

@dataclass
class CLVRecord:
    trade_id: str
    market_id: str
    outcome_id: str
    side: str
    entry_price: float
    entry_time: datetime
    price_after_15m: Optional[float] = None
    price_after_1h: Optional[float] = None
    pre_event_price: Optional[float] = None
    closing_price: Optional[float] = None
    settlement_price: Optional[float] = None
    missing_market_price: Optional[bool] = None
    missing_market_price_checkpoint: Optional[str] = None
    missing_market_price_reason: Optional[str] = None
    last_clv_update_attempt: Optional[str] = None
    clv_update_error: Optional[str] = None

def init_clv_record(
    trade_id: str,
    market_id: str,
    outcome_id: str,
    side: str,
    entry_price: float,
    entry_time: datetime
) -> CLVRecord:
    return CLVRecord(
        trade_id=trade_id,
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        entry_price=entry_price,
        entry_time=entry_time
    )

def log_clv_record(record: CLVRecord, filepath: str = "clv_tracking.jsonl") -> None:
    data = {
        "trade_id": record.trade_id,
        "market_id": record.market_id,
        "outcome_id": record.outcome_id,
        "side": record.side,
        "entry_price": record.entry_price,
        "entry_time": record.entry_time.isoformat(),
        "price_after_15m": record.price_after_15m,
        "price_after_1h": record.price_after_1h,
        "pre_event_price": record.pre_event_price,
        "closing_price": record.closing_price,
        "settlement_price": record.settlement_price,
        "missing_market_price": record.missing_market_price,
        "missing_market_price_checkpoint": record.missing_market_price_checkpoint,
        "missing_market_price_reason": record.missing_market_price_reason,
        "last_clv_update_attempt": record.last_clv_update_attempt,
        "clv_update_error": record.clv_update_error
    }
    with open(filepath, "a") as f:
        f.write(json.dumps(data) + "\n")
