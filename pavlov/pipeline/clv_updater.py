"""CLV checkpoint updater — jsonl legacy + durable Supabase clv_obligations."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Awaitable, List, Optional

from loguru import logger

from pavlov.pipeline.clv_tracker import CLVRecord

# After a due time, keep retrying until this grace expires, then mark unavailable.
_OVERDUE_GRACE = timedelta(minutes=30)


def load_clv_records(filepath: str = "clv_tracking.jsonl") -> List[CLVRecord]:
    records = []
    try:
        with open(filepath, "r") as f:
            for line in f:
                data = json.loads(line)
                market = data.get("entry_market_price", data.get("entry_price"))
                effective = data.get("entry_effective_cost", market)
                rec = CLVRecord(
                    trade_id=data["trade_id"],
                    market_id=data["market_id"],
                    outcome_id=data["outcome_id"],
                    side=data["side"],
                    entry_time=datetime.fromisoformat(data["entry_time"]),
                    entry_market_price=float(market),
                    entry_effective_cost=float(effective),
                    entry_price=float(market),
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
                "entry_market_price": r.entry_market_price,
                "entry_effective_cost": r.entry_effective_cost,
                "entry_price": r.entry_market_price,
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
                "clv_update_error": r.clv_update_error,
            }
            f.write(json.dumps(data) + "\n")


def calculate_market_clv(
    entry_market_price: float, current_price: float, side: str
) -> float:
    """Market CLV: current executable − entry market fill (simulated_fill_price)."""
    return current_price - entry_market_price


def calculate_execution_adjusted_clv(
    entry_effective_cost: float, current_price: float, side: str
) -> float:
    """Execution-adjusted CLV: current executable − entry effective cost (limit_price)."""
    return current_price - entry_effective_cost


def calculate_clv(entry_price: float, current_price: float, side: str) -> float:
    """Legacy alias for market CLV."""
    return calculate_market_clv(entry_price, current_price, side)

def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _checkpoint_fields(checkpoint: str) -> tuple[str, str, str, str]:
    """Return (status_col, price_col, obs_ts_col, book_ts_col)."""
    if checkpoint == "15m":
        return "status_15m", "obs_15m_price", "obs_15m_ts", "book_ts_15m"
    if checkpoint == "1h":
        return "status_1h", "obs_1h_price", "obs_1h_ts", "book_ts_1h"
    if checkpoint == "close":
        return "status_close", "obs_close_price", "obs_close_ts", "book_ts_close"
    raise ValueError(f"unknown checkpoint {checkpoint}")


async def update_clv_checkpoints(
    fetch_price: Callable[[str, str, str], Any],
    filepath: str = "clv_tracking.jsonl",
) -> None:
    """Legacy jsonl updater (kept for local artifacts). Prefer update_clv_obligations."""
    records = load_clv_records(filepath)
    now = datetime.now(timezone.utc)
    updated = False

    for r in records:
        entry = r.entry_time
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=timezone.utc)
        age_delta = now - entry

        if r.price_after_15m is None and age_delta >= timedelta(minutes=15):
            p = await fetch_price(r.market_id, r.outcome_id, r.side)
            r.last_clv_update_attempt = now.isoformat()
            if p is not None:
                r.price_after_15m = p
                r.missing_market_price = False
                logger.info(
                    f"Updated AFTER_15M for {r.trade_id}: {p} "
                    f"(market_CLV: {calculate_market_clv(r.entry_market_price, p, r.side)}, "
                    f"exec_CLV: {calculate_execution_adjusted_clv(r.entry_effective_cost, p, r.side)})"
                )
                updated = True
            else:
                if not r.missing_market_price:
                    r.missing_market_price = True
                    r.missing_market_price_checkpoint = "AFTER_15M"
                    r.missing_market_price_reason = "NO_ORDERBOOK_PRICE"
                    r.clv_update_error = "Failed to fetch price"
                    updated = True

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

    if updated:
        save_clv_records(records, filepath)


FetchPriceResult = tuple[Optional[float], Optional[datetime]]
FetchPriceFn = Callable[[str, str, str], Awaitable[FetchPriceResult | float | None]]


async def _normalize_fetch(
    fetch_price: FetchPriceFn,
    market_id: str,
    outcome_id: str,
    side: str,
) -> FetchPriceResult:
    """Normalize fetch_price returns to (executable_price, book_ts)."""
    result = await fetch_price(market_id, outcome_id, side)
    if result is None:
        return None, None
    if isinstance(result, (int, float)):
        return float(result), None
    if isinstance(result, tuple):
        price = result[0] if len(result) > 0 else None
        book_ts = result[1] if len(result) > 1 else None
        return (float(price) if price is not None else None), _parse_ts(book_ts)
    return None, None


def count_clv_obligations(db=None) -> dict[str, int]:
    """Return total + per-status counts for reporting."""
    from backend.db import get_db

    db = db or get_db()
    rows = db.table("clv_obligations").select(
        "status_15m,status_1h,status_close"
    ).execute().data or []
    out = {
        "total": len(rows),
        "pending_15m": sum(1 for r in rows if r.get("status_15m") == "pending"),
        "pending_1h": sum(1 for r in rows if r.get("status_1h") == "pending"),
        "pending_close": sum(1 for r in rows if r.get("status_close") == "pending"),
        "observed_15m": sum(1 for r in rows if r.get("status_15m") == "observed"),
        "observed_1h": sum(1 for r in rows if r.get("status_1h") == "observed"),
        "observed_close": sum(1 for r in rows if r.get("status_close") == "observed"),
        "unavailable_15m": sum(1 for r in rows if r.get("status_15m") == "unavailable"),
        "unavailable_1h": sum(1 for r in rows if r.get("status_1h") == "unavailable"),
        "unavailable_close": sum(1 for r in rows if r.get("status_close") == "unavailable"),
    }
    return out


async def update_clv_obligations(
    fetch_price: FetchPriceFn,
    *,
    db=None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """
    Consume pending clv_obligations rows and write 15m / 1h / close observations.

    ``fetch_price(market_id, outcome_id, side)`` must return the side-correct
    executable price for the purchased outcome token (ask when buying YES/token).
    May return ``(price, book_timestamp)`` or bare ``price``.
    """
    from backend.db import get_db

    db = db or get_db()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    rows = (
        db.table("clv_obligations")
        .select("*")
        .or_("status_15m.eq.pending,status_1h.eq.pending,status_close.eq.pending")
        .execute()
        .data
        or []
    )

    stats = {"checked": 0, "updated": 0, "unavailable": 0, "errors": 0}

    for row in rows:
        stats["checked"] += 1
        candidate_id = row.get("candidate_id")
        market_id = row.get("market_id") or ""
        outcome_id = row.get("outcome_id") or ""
        side = (row.get("side") or "YES").upper()
        patch: dict[str, Any] = {"updated_at": now.isoformat()}
        touched = False

        for checkpoint, due_key in (
            ("15m", "due_15m"),
            ("1h", "due_1h"),
            ("close", "due_close"),
        ):
            status_col, price_col, obs_ts_col, book_ts_col = _checkpoint_fields(checkpoint)
            if row.get(status_col) != "pending":
                continue
            due = _parse_ts(row.get(due_key))
            if due is None:
                # close may be unset until event time known — skip without failing
                if checkpoint == "close":
                    continue
                # 15m/1h should always have due times; mark unavailable if missing
                patch[status_col] = "unavailable"
                patch[obs_ts_col] = now.isoformat()
                meta = dict(row.get("metadata") or {})
                meta[f"{checkpoint}_reason"] = "MISSING_DUE_TIME"
                patch["metadata"] = meta
                touched = True
                stats["unavailable"] += 1
                continue
            if now < due:
                continue

            try:
                price, book_ts = await _normalize_fetch(
                    fetch_price, market_id, outcome_id, side
                )
            except Exception as exc:
                logger.warning(
                    f"CLV fetch error {candidate_id} {checkpoint}: {exc}"
                )
                price, book_ts = None, None
                stats["errors"] += 1

            if price is not None and 0.0 < float(price) < 1.0:
                patch[status_col] = "observed"
                patch[price_col] = float(price)
                patch[obs_ts_col] = now.isoformat()
                if book_ts is not None:
                    patch[book_ts_col] = book_ts.isoformat()
                touched = True
                stats["updated"] += 1
                logger.info(
                    f"CLV {checkpoint} observed for {candidate_id}: {price}"
                )
            elif now > due + _OVERDUE_GRACE:
                patch[status_col] = "unavailable"
                patch[obs_ts_col] = now.isoformat()
                meta = dict(row.get("metadata") or {})
                meta[f"{checkpoint}_reason"] = "NO_ORDERBOOK_PRICE_OVERDUE"
                patch["metadata"] = meta
                touched = True
                stats["unavailable"] += 1
                logger.warning(
                    f"CLV {checkpoint} unavailable (overdue) for {candidate_id}"
                )
            # else: still within grace — leave pending and retry next cycle

        if touched:
            try:
                (
                    db.table("clv_obligations")
                    .update(patch)
                    .eq("candidate_id", candidate_id)
                    .execute()
                )
            except Exception as exc:
                stats["errors"] += 1
                logger.error(f"CLV obligation write failed for {candidate_id}: {exc}")
                raise

    return stats
