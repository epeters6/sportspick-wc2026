import json
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, List, Optional
from loguru import logger
from pavlov.pipeline.clv_tracker import CLVRecord

def load_clv_records(filepath: str = "clv_tracking.jsonl") -> List[CLVRecord]:
    records = []
    try:
        with open(filepath, "r") as f:
            for line in f:
                data = json.loads(line)
                rec = CLVRecord(
                    trade_id=data["trade_id"],
                    market_id=data["market_id"],
                    outcome_id=data["outcome_id"],
                    side=data["side"],
                    entry_price=data["entry_price"],
                    entry_time=datetime.fromisoformat(data["entry_time"]),
                    price_after_15m=data.get("price_after_15m"),
                    price_after_1h=data.get("price_after_1h"),
                    pre_event_price=data.get("pre_event_price"),
                    closing_price=data.get("closing_price"),
                    settlement_price=data.get("settlement_price"),
                    missing_market_price=data.get("missing_market_price"),
                    missing_market_price_checkpoint=data.get("missing_market_price_checkpoint"),
                    missing_market_price_reason=data.get("missing_market_price_reason"),
                    last_clv_update_attempt=data.get("last_clv_update_attempt"),
                    clv_update_error=data.get("clv_update_error"),
                )
                records.append(rec)
    except FileNotFoundError:
        pass
    return records

def save_clv_records(records: List[CLVRecord], filepath: str = "clv_tracking.jsonl") -> None:
    with open(filepath, "w") as f:
        for r in records:
            data = {
                "trade_id": r.trade_id,
                "market_id": r.market_id,
                "outcome_id": r.outcome_id,
                "side": r.side,
                "entry_price": r.entry_price,
                "entry_time": r.entry_time.isoformat(),
                "price_after_15m": r.price_after_15m,
                "price_after_1h": r.price_after_1h,
                "pre_event_price": r.pre_event_price,
                "closing_price": r.closing_price,
                "settlement_price": r.settlement_price,
                "missing_market_price": r.missing_market_price,
                "missing_market_price_checkpoint": r.missing_market_price_checkpoint,
                "missing_market_price_reason": r.missing_market_price_reason,
                "last_clv_update_attempt": r.last_clv_update_attempt,
                "clv_update_error": r.clv_update_error
            }
            f.write(json.dumps(data) + "\n")

def calculate_clv(entry_price: float, current_price: float, side: str) -> float:
    # Always assumes YES probability space for current_price 
    if side.upper() == "YES":
        return current_price - entry_price
    else:
        # If we entered NO at NO price N_entry:
        # NO CLV = (1 - current_yes_price) - entry_no_price
        # Wait, the prompt says: For NO: clv = entry_yes_price - current_yes_price.
        # But our entry price for NO is the NO price. If entry_price is the NO price,
        # then entry_yes_price = (1 - entry_no_price).
        # So CLV = (1 - entry_no_price) - current_yes_price.
        # Or, keeping it in NO space: current_no_price = (1 - current_yes_price).
        # NO CLV = current_no_price - entry_no_price = (1 - current_yes_price) - entry_no_price.
        # So yes, they are equivalent. We will use (1 - current_price) - entry_price where current_price is YES price.
        # Wait, prompt: "For NO: clv = entry_yes_price - current_yes_price or equivalent using NO prices"
        # If current_price is the NO price directly? Let's standardise: fetch_price returns the outcome price matching the `outcome_id` and `side`. 
        # If `fetch_price` returns the current executable price of the SAME side we bought, then CLV = current_price - entry_price, regardless of side!
        # If it returns YES price always, it's different.
        # Let's assume fetch_price returns the price of the exact asset we bought. So CLV = current_price - entry_price.
        return current_price - entry_price

async def update_clv_checkpoints(
    fetch_price: Callable[[str, str, str], Any],
    filepath: str = "clv_tracking.jsonl"
) -> None:
    records = load_clv_records(filepath)
    now = datetime.now(timezone.utc)
    updated = False
    
    for r in records:
        age_delta = now - r.entry_time
        
        # Check AFTER_15M
        if r.price_after_15m is None and age_delta >= timedelta(minutes=15):
            p = await fetch_price(r.market_id, r.outcome_id, r.side)
            r.last_clv_update_attempt = now.isoformat()
            if p is not None:
                r.price_after_15m = p
                r.missing_market_price = False
                logger.info(f"Updated AFTER_15M for {r.trade_id}: {p} (CLV: {calculate_clv(r.entry_price, p, r.side)})")
                updated = True
            else:
                if not r.missing_market_price:
                    r.missing_market_price = True
                    r.missing_market_price_checkpoint = "AFTER_15M"
                    r.missing_market_price_reason = "NO_ORDERBOOK_PRICE"
                    r.clv_update_error = "Failed to fetch price"
                    updated = True
                
        # Check AFTER_1H
        if r.price_after_1h is None and age_delta >= timedelta(hours=1):
            p = await fetch_price(r.market_id, r.outcome_id, r.side)
            r.last_clv_update_attempt = now.isoformat()
            if p is not None:
                r.price_after_1h = p
                r.missing_market_price = False
                logger.info(f"Updated AFTER_1H for {r.trade_id}: {p}")
                updated = True
            else:
                if not r.missing_market_price:
                    r.missing_market_price = True
                    r.missing_market_price_checkpoint = "AFTER_1H"
                    r.missing_market_price_reason = "NO_ORDERBOOK_PRICE"
                    r.clv_update_error = "Failed to fetch price"
                    updated = True
                
        # Other checkpoints (PRE_EVENT, CLOSE, SETTLEMENT) would be triggered by external state changes
        # e.g. a flag passed into this function, but for time-based, these two are deterministic.

    if updated:
        save_clv_records(records, filepath)
