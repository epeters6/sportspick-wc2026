"""
pipeline/order_manager.py – Order placement and skip logging.

Public API
----------
async place_trade(signal, kalshi_client) -> dict
      log_skip(signal)
      log_signal_watch(signal, reason)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

try:
    from pavlov import data_paths as dp
    from pavlov.config import CONFIG
    from pavlov.pipeline import signal_learning_log
    from pavlov.pipeline.execution_ledger import begin_attempt, complete_attempt
except ModuleNotFoundError as exc:
    if exc.name != "pavlov":
        raise
    import data_paths as dp
    from config import CONFIG
    from pipeline import signal_learning_log
    from pipeline.execution_ledger import begin_attempt, complete_attempt

logger = logging.getLogger(__name__)

_POSITIONS_FILE = os.path.join(dp.logs_dir(), "positions.json")
_SIGNALS_FILE   = os.path.join(dp.logs_dir(), "signals.json")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def place_trade(signal: dict, kalshi_client) -> dict:
    """Place a limit order on Kalshi for the given signal.

    Args:
        signal:        Signal dict from signal_engine.calculate_edge().
        kalshi_client: The kalshi_client module (provides place_order()).

    Accepted orders remain pending until venue fills and fees are reconciled.
    No accepted order is treated as a fully filled position.
    """
    ticker          = signal["ticker"]
    side            = signal["recommended_side"]
    contracts       = signal["kelly_contracts"]
    # Price above the implied ask so the limit crosses or lifts the book instead of
    # sitting at the touch (was +1¢; configurable via AUTO_BET_PRICE_BUFFER_CENTS).
    buf = int(CONFIG.get("AUTO_BET_PRICE_BUFFER_CENTS") or 5)
    buf = max(0, min(50, buf))
    raw_price = (
        signal["implied_prob"] * 100 if side == "yes"
        else (1 - signal["implied_prob"]) * 100
    )
    price_cents = max(1, min(99, round(raw_price) + buf))

    try:
        attempt_id = begin_attempt(
            _POSITIONS_FILE, signal, venue="kalshi", quantity=float(contracts), price=price_cents / 100,
        )
    except (OSError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    try:
        # place_order polls with time.sleep() — run in a thread so it doesn't
        # block the asyncio event loop and starve Discord interaction callbacks.
        result = await asyncio.to_thread(
            kalshi_client.place_order,
            ticker=ticker,
            side=side,
            contracts=contracts,
            price_cents=price_cents,
        )
    except Exception as exc:
        err = str(exc)
        logger.error("OrderManager: place_order raised – %s", err)
        result = {"status": "submission_unknown", "error": err}
    return complete_attempt(_POSITIONS_FILE, attempt_id, result)


def log_skip(signal: dict) -> None:
    """Append a skip record to /logs/signals.json.

    Stores the full forecast context so the learning loop can later check
    the market outcome and update ensemble bias — even though no bet was placed.
    A gentle station-score nudge is applied after settlement from the
    hypothetical result (weaker than real-money trades).

    Args:
        signal: Signal dict from signal_engine.calculate_edge().
    """
    if signal_learning_log.append_learning_record(
        _SIGNALS_FILE,
        signal,
        action="skip",
        learn_reason="discord_skip",
        learn_source="discord",
        venue="kalshi",
    ):
        logger.info("OrderManager: skip logged for %s.", signal.get("ticker"))


def log_signal_watch(signal: dict, reason: str) -> None:
    """Log an actionable signal that did not open a position (e.g. auto-bet rejected).

    Same downstream learning as Discord skips: ensemble bias, calibration,
    and soft station-score update from ``would_have_won`` after METAR actuals exist.
    """
    if signal_learning_log.append_learning_record(
        _SIGNALS_FILE,
        signal,
        action="signal_watch",
        learn_reason=(reason or "").strip(),
        learn_source="auto",
        venue="kalshi",
    ):
        logger.info(
            "OrderManager: signal_watch logged for %s — %s",
            signal.get("ticker"),
            (reason or "")[:160],
        )
