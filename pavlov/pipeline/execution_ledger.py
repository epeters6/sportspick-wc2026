"""Persist live order attempts separately from settled-position accounting.

The legacy learning loops cannot reconcile fills, fees, and resting orders. Hold
these attempts for venue reconciliation, and block new orders in that ledger
until reconciliation explicitly clears the hold. Never retry an uncertain send.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _read(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as source:
            rows = json.load(source)
    except FileNotFoundError:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("INVALID_EXECUTION_LEDGER: expected a list of records")
    return rows


def _write(path: str, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def begin_attempt(path: str, signal: dict, *, venue: str, quantity: float, price: float) -> str:
    """Durably reserve an attempt before sending; unreadable ledgers fail closed."""
    if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(price) or not 0 < price < 1:
        raise ValueError("INVALID_ORDER: positive quantity and a price between zero and one required")
    guard = Path(f"{path}.submission.lock")
    guard.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("ORDER_RECONCILIATION_REQUIRED: an execution ledger submission lock already exists") from exc
    try:
        os.close(descriptor)
        return _reserve_attempt(path, signal, venue=venue, quantity=quantity, price=price)
    finally:
        guard.unlink()


def _reserve_attempt(path: str, signal: dict, *, venue: str, quantity: float, price: float) -> str:
    rows = _read(path)
    if any(row.get("requires_order_reconciliation") or row.get("status") == "pending_order" for row in rows):
        raise ValueError("ORDER_RECONCILIATION_REQUIRED: an earlier order has unresolved execution or fees")
    attempt_id = str(uuid4())
    rows.append({
        **signal,
        "venue": venue,
        "execution_attempt_id": attempt_id,
        "order_id": None,
        "requested_contracts": quantity,
        "kelly_contracts": 0.0,
        "filled_contracts": 0.0,
        "remaining_contracts": quantity,
        "limit_price": price,
        "reserved_notional": quantity * price,
        "status": "pending_order",
        "order_status": "submission_pending",
        "requires_order_reconciliation": True,
        "placed_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": None,
        "pl": None,
    })
    _write(path, rows)
    return attempt_id


def complete_attempt(path: str, attempt_id: str, result: dict) -> dict:
    """Preserve venue-reported fills without admitting them to legacy P&L."""
    rows = _read(path)
    attempt = next(row for row in rows if row.get("execution_attempt_id") == attempt_id)
    try:
        fills = float(result.get("filled_contracts", 0))
        if not math.isfinite(fills) or fills < 0:
            raise ValueError("invalid fills")
    except (TypeError, ValueError):
        fills = 0.0
        result = {**result, "execution_data_error": "INVALID_FILL_QUANTITY"}
    attempt.update({
        "order_id": result.get("order_id"),
        "order_status": result.get("order_status") or result.get("status") or "unknown",
        "filled_contracts": fills,
        "kelly_contracts": fills,
        "remaining_contracts": result.get("remaining_contracts"),
        "average_fill_price": result.get("average_fill_price"),
        "fees_paid": result.get("fees_paid"),
        "execution_result": result,
        "execution_updated_at": datetime.now(timezone.utc).isoformat(),
    })
    # A timeout or an accepted GTC order may still fill. Preserve the conservative
    # reservation, including after partial fills; only venue reconciliation clears it.
    _write(path, rows)
    return {
        "success": False,
        "pending": True,
        "order_id": result.get("order_id"),
        "filled_contracts": fills,
        "execution_attempt_id": attempt_id,
        "error": "ORDER_RECONCILIATION_REQUIRED: order attempt recorded; verify venue fills, remaining orders, and fees before another submission",
    }
