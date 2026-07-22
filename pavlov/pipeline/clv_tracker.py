from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta, timezone
import json
import logging

logger = logging.getLogger(__name__)


@dataclass
class CLVRecord:
    trade_id: str
    market_id: str
    outcome_id: str
    side: str
    entry_time: datetime
    # Market fill (simulated_fill_price / best ask) — used for market CLV
    entry_market_price: float
    # Effective cost (limit_price = ask + fee + slippage) — execution-adjusted CLV
    entry_effective_cost: float
    # Legacy alias: same as entry_market_price (jsonl / older readers)
    entry_price: Optional[float] = None
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

    def __post_init__(self) -> None:
        if self.entry_price is None:
            self.entry_price = float(self.entry_market_price)


def _upsert_clv_obligation(
    record: CLVRecord,
    platform: Optional[str] = None,
    due_close: Optional[datetime] = None,
) -> None:
    """Durable CLV checkpoint stub in Supabase. Fail soft if unavailable."""
    try:
        from backend.db import get_db

        entry = record.entry_time
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=timezone.utc)
        close = due_close
        if close is not None and close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        market_px = float(record.entry_market_price)
        effective = float(record.entry_effective_cost)
        row = {
            "candidate_id": record.trade_id,
            "platform": platform or "unknown",
            "market_id": record.market_id,
            "outcome_id": record.outcome_id,
            "side": record.side,
            "entry_price": market_px,  # legacy column = market fill
            "entry_market_price": market_px,
            "entry_effective_cost": effective,
            "entry_ts": entry.isoformat(),
            "due_15m": (entry + timedelta(minutes=15)).isoformat(),
            "due_1h": (entry + timedelta(hours=1)).isoformat(),
            "due_close": close.isoformat() if close is not None else None,
            "status_15m": "pending",
            "status_1h": "pending",
            "status_close": "pending",
        }
        # Soft-fail if new columns not yet migrated: retry without them
        try:
            get_db().table("clv_obligations").upsert(row, on_conflict="candidate_id").execute()
        except Exception as col_exc:
            if "entry_market_price" in str(col_exc) or "entry_effective_cost" in str(col_exc):
                row.pop("entry_market_price", None)
                row.pop("entry_effective_cost", None)
                get_db().table("clv_obligations").upsert(row, on_conflict="candidate_id").execute()
            else:
                raise
    except Exception as exc:
        logger.debug("clv_obligations upsert skipped: %s", exc)


def init_clv_record(
    trade_id: str,
    market_id: str,
    outcome_id: str,
    side: str,
    entry_price: float,
    entry_time: datetime,
    platform: Optional[str] = None,
    due_close: Optional[datetime] = None,
    *,
    entry_market_price: Optional[float] = None,
    entry_effective_cost: Optional[float] = None,
) -> CLVRecord:
    """
    Create a CLV record.

    Positional ``entry_price`` is the legacy market-fill argument.
    Prefer also passing ``entry_market_price`` (= simulated_fill_price) and
    ``entry_effective_cost`` (= limit_price) so both CLV variants are stored.
    """
    market = float(
        entry_market_price if entry_market_price is not None else entry_price
    )
    effective = float(
        entry_effective_cost if entry_effective_cost is not None else entry_price
    )
    rec = CLVRecord(
        trade_id=trade_id,
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        entry_time=entry_time,
        entry_market_price=market,
        entry_effective_cost=effective,
    )
    _upsert_clv_obligation(rec, platform=platform, due_close=due_close)
    return rec


def log_clv_record(record: CLVRecord, filepath: str = "clv_tracking.jsonl") -> None:
    data = {
        "trade_id": record.trade_id,
        "market_id": record.market_id,
        "outcome_id": record.outcome_id,
        "side": record.side,
        "entry_market_price": record.entry_market_price,
        "entry_effective_cost": record.entry_effective_cost,
        "entry_price": record.entry_market_price,  # legacy alias
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
        "clv_update_error": record.clv_update_error,
    }
    with open(filepath, "a") as f:
        f.write(json.dumps(data) + "\n")
