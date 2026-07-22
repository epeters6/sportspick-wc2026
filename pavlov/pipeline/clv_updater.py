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
    *,
    now: Optional[datetime] = None,
    overdue_grace: timedelta = _OVERDUE_GRACE,
) -> None:
    """Legacy jsonl updater (kept for local artifacts). Prefer update_clv_obligations."""
    records = load_clv_records(filepath)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    updated = False

    for r in records:
        entry = r.entry_time
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=timezone.utc)

        for attr, due_delta, checkpoint in (
            ("price_after_15m", timedelta(minutes=15), "AFTER_15M"),
            ("price_after_1h", timedelta(hours=1), "AFTER_1H"),
        ):
            if getattr(r, attr) is not None:
                continue
            due = entry + due_delta
            if now < due:
                continue
            r.last_clv_update_attempt = now.isoformat()
            delay = now - due
            if delay > overdue_grace:
                # Price may exist, but observation is too late to label as 15m/1h
                if not r.missing_market_price:
                    r.missing_market_price = True
                    r.missing_market_price_checkpoint = checkpoint
                    r.missing_market_price_reason = "OBSERVATION_OVERDUE"
                    r.clv_update_error = (
                        f"Observation delay {delay.total_seconds():.0f}s exceeds grace"
                    )
                    updated = True
                continue
            p = await fetch_price(r.market_id, r.outcome_id, r.side)
            if isinstance(p, tuple):
                p = p[0]
            if p is not None:
                setattr(r, attr, p)
                r.missing_market_price = False
                logger.info(f"Updated {checkpoint} for {r.trade_id}: {p}")
                updated = True
            # else: within grace, leave unset and retry

    if updated:
        save_clv_records(records, filepath)


FetchPriceResult = tuple[Optional[float], Optional[datetime], Optional[datetime]]
# (executable_price, book_timestamp, received_timestamp)
FetchPriceFn = Callable[
    [str, str, str], Awaitable[FetchPriceResult | float | None | tuple]
]

_CLOSE_LEAD = timedelta(minutes=5)


async def _normalize_fetch(
    fetch_price: FetchPriceFn,
    market_id: str,
    outcome_id: str,
    side: str,
) -> FetchPriceResult:
    """Normalize fetch_price returns to (price, book_ts, received_ts)."""
    result = await fetch_price(market_id, outcome_id, side)
    if result is None:
        return None, None, None
    if isinstance(result, (int, float)):
        return float(result), None, None
    if isinstance(result, tuple):
        price = result[0] if len(result) > 0 else None
        book_ts = result[1] if len(result) > 1 else None
        received_ts = result[2] if len(result) > 2 else None
        return (
            (float(price) if price is not None else None),
            _parse_ts(book_ts),
            _parse_ts(received_ts),
        )
    return None, None, None


def _event_start_for_close(row: dict, due: datetime, meta: dict) -> datetime:
    """First-pitch time for close checkpoint (due_close is typically T−5m)."""
    start = _parse_ts(meta.get("event_start_utc")) or _parse_ts(row.get("due_close_event_start"))
    if start is not None:
        return start
    # Legacy rows stored due_close = first pitch; new rows store due_close = T−5m.
    # Prefer metadata; otherwise assume due is already the lead-adjusted time.
    lead = meta.get("close_lead_minutes")
    if lead is not None:
        return due + timedelta(minutes=float(lead))
    # Heuristic: if due looks like first pitch (legacy), use due; else due + 5m.
    return due + _CLOSE_LEAD


def _accept_book_for_platform(
    platform: str,
    price: Optional[float],
    book_ts: Optional[datetime],
    received_ts: Optional[datetime],
) -> tuple[bool, Optional[str]]:
    """
    Polymarket requires CLOB book_ts. Kalshi shadow may use receipt-only.
    Returns (ok, reject_reason).
    """
    if price is None or not (0.0 < float(price) < 1.0):
        return False, None
    plat = (platform or "").lower()
    if plat == "polymarket":
        if book_ts is None:
            return False, "MISSING_ORDERBOOK_TIMESTAMP"
        return True, None
    # Kalshi / unknown: prefer book_ts; allow receipt fallback for shadow freshness
    if book_ts is None and received_ts is None:
        return False, "MISSING_RECEIVED_TIMESTAMP"
    return True, None


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
    overdue_grace: timedelta = _OVERDUE_GRACE,
) -> dict[str, int]:
    """
    Consume pending clv_obligations rows and write 15m / 1h / close observations.

    ``fetch_price`` should return ``(price, book_timestamp, received_timestamp)``.
    ``obs_*_ts`` / receipt metadata use the HTTP fetch-boundary received_timestamp
    when present — never a single batch wall-clock for all rows.

    Close observations are only accepted in the pre-start window
    (``due_close`` … first pitch). Any post-start book is rejected.
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
        platform = (row.get("platform") or "").lower()
        patch: dict[str, Any] = {"updated_at": now.isoformat()}
        touched = False
        meta = dict(row.get("metadata") or {})

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
                if checkpoint == "close":
                    continue
                patch[status_col] = "unavailable"
                patch[obs_ts_col] = now.isoformat()
                meta[f"{checkpoint}_reason"] = "MISSING_DUE_TIME"
                meta[f"{checkpoint}_receipt_ts"] = now.isoformat()
                patch["metadata"] = meta
                touched = True
                stats["unavailable"] += 1
                continue

            event_start = None
            if checkpoint == "close":
                event_start = _event_start_for_close(row, due, meta)
                # Legacy: due_close was first pitch → treat due as event_start and
                # open the window at T−5m.
                if meta.get("event_start_utc") is None and meta.get("close_lead_minutes") is None:
                    # Ambiguous legacy: if due == stored first pitch, window is [due-5m, due)
                    event_start = due
                    due = event_start - _CLOSE_LEAD
                if now < due:
                    continue
                if now >= event_start:
                    patch[status_col] = "unavailable"
                    patch[obs_ts_col] = now.isoformat()
                    meta[f"{checkpoint}_reason"] = "POST_START"
                    meta[f"{checkpoint}_receipt_ts"] = now.isoformat()
                    meta[f"{checkpoint}_event_start_utc"] = event_start.isoformat()
                    patch["metadata"] = meta
                    touched = True
                    stats["unavailable"] += 1
                    logger.warning(
                        f"CLV close unavailable (post-start) for {candidate_id}"
                    )
                    continue
            else:
                if now < due:
                    continue

            delay = now - due

            # 15m/1h: overdue beyond grace → unavailable even if price available
            if checkpoint != "close" and delay > overdue_grace:
                price_probe = book_ts_probe = recv_probe = None
                try:
                    price_probe, book_ts_probe, recv_probe = await _normalize_fetch(
                        fetch_price, market_id, outcome_id, side
                    )
                except Exception as exc:
                    logger.warning(
                        f"CLV overdue probe error {candidate_id} {checkpoint}: {exc}"
                    )
                    stats["errors"] += 1
                receipt = recv_probe or now
                patch[status_col] = "unavailable"
                patch[obs_ts_col] = receipt.isoformat()
                meta[f"{checkpoint}_reason"] = "OBSERVATION_OVERDUE"
                meta[f"{checkpoint}_obs_delay_seconds"] = (
                    receipt - due
                ).total_seconds()
                meta[f"{checkpoint}_receipt_ts"] = receipt.isoformat()
                meta[f"{checkpoint}_price_available_but_late"] = (
                    price_probe is not None and 0.0 < float(price_probe) < 1.0
                )
                if price_probe is not None:
                    meta[f"{checkpoint}_late_price_not_accepted"] = float(price_probe)
                if book_ts_probe is not None:
                    meta[f"{checkpoint}_late_book_ts"] = book_ts_probe.isoformat()
                patch["metadata"] = meta
                touched = True
                stats["unavailable"] += 1
                logger.warning(
                    f"CLV {checkpoint} unavailable (overdue) for {candidate_id}; "
                    f"price_available={meta[f'{checkpoint}_price_available_but_late']}"
                )
                continue

            try:
                price, book_ts, received_ts = await _normalize_fetch(
                    fetch_price, market_id, outcome_id, side
                )
            except Exception as exc:
                logger.warning(
                    f"CLV fetch error {candidate_id} {checkpoint}: {exc}"
                )
                price, book_ts, received_ts = None, None, None
                stats["errors"] += 1

            receipt = received_ts or now
            # Reject every post-start book for close (and any stamp after first pitch)
            if checkpoint == "close" and event_start is not None:
                stamp = book_ts or received_ts
                if stamp is not None and stamp >= event_start:
                    patch[status_col] = "unavailable"
                    patch[obs_ts_col] = receipt.isoformat()
                    meta[f"{checkpoint}_reason"] = "POST_START_BOOK"
                    meta[f"{checkpoint}_receipt_ts"] = receipt.isoformat()
                    meta[f"{checkpoint}_event_start_utc"] = event_start.isoformat()
                    if book_ts is not None:
                        meta[f"{checkpoint}_rejected_book_ts"] = book_ts.isoformat()
                    patch["metadata"] = meta
                    touched = True
                    stats["unavailable"] += 1
                    logger.warning(
                        f"CLV close rejected post-start book for {candidate_id}"
                    )
                    continue

            ok, reject_reason = _accept_book_for_platform(
                platform, price, book_ts, received_ts
            )
            if ok and price is not None:
                patch[status_col] = "observed"
                patch[price_col] = float(price)
                patch[obs_ts_col] = receipt.isoformat()
                if book_ts is not None:
                    patch[book_ts_col] = book_ts.isoformat()
                meta[f"{checkpoint}_reason"] = "OBSERVED"
                meta[f"{checkpoint}_obs_delay_seconds"] = (
                    receipt - due
                ).total_seconds()
                meta[f"{checkpoint}_receipt_ts"] = receipt.isoformat()
                meta[f"{checkpoint}_book_ts_source"] = (
                    "orderbook_timestamp" if book_ts is not None else "received_timestamp"
                )
                patch["metadata"] = meta
                touched = True
                stats["updated"] += 1
                logger.info(
                    f"CLV {checkpoint} observed for {candidate_id}: {price} "
                    f"(receipt={receipt.isoformat()})"
                )
            elif reject_reason == "MISSING_ORDERBOOK_TIMESTAMP" and platform == "polymarket":
                # Within grace for 15m/1h: leave pending so a later stamp can land.
                # For close (window ends at first pitch), mark unavailable if no stamp.
                if checkpoint == "close":
                    patch[status_col] = "unavailable"
                    patch[obs_ts_col] = receipt.isoformat()
                    meta[f"{checkpoint}_reason"] = reject_reason
                    meta[f"{checkpoint}_receipt_ts"] = receipt.isoformat()
                    patch["metadata"] = meta
                    touched = True
                    stats["unavailable"] += 1
                # else: leave pending
            # else: still within grace / no price — leave pending

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
