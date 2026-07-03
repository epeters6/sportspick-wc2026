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

import data_paths as dp
from config import CONFIG
from pipeline import signal_learning_log

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

    Returns:
        {"success": True,  "order_id": str}   on success
        {"success": False, "error":    str}   on failure
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
        return {"success": False, "error": err}

    # place_order returns {"order_id", "status", "filled_contracts", "price"}
    status = result.get("status", "error")
    if status == "error" or not result.get("order_id"):
        err = result.get("error", f"unexpected status: {status!r}")
        logger.error("OrderManager: order failed for %s – %s", ticker, err)
        return {"success": False, "error": err}

    # ── Persist position ──────────────────────────────────────────────
    positions = _load_json(_POSITIONS_FILE, [])
    positions.append(
        {
            "order_id":         result["order_id"],
            "ticker":           ticker,
            "city":             signal.get("city", ""),
            "metric":           signal.get("metric", ""),
            "direction":        signal.get("direction", ""),
            "threshold_f":      signal.get("threshold_f"),
            "market_date":      signal.get("market_date", ""),
            "nws_predicted":    signal.get("nws_predicted"),
            "ensemble_mean":    signal.get("ensemble_mean"),
            "ensemble_spread":  signal.get("ensemble_spread"),
            "days_out":         signal.get("days_out", 0),
            "station":          signal.get("station", ""),
            "recommended_side": side,
            "kelly_contracts":  contracts,
            "price_cents":      price_cents,
            "placed_at":        datetime.now(timezone.utc).isoformat(),
            "status":           "open",
            "edge":             signal.get("edge"),
            "model_prob":       signal.get("model_prob"),
            "placed_via":       signal.get("placed_via", "manual"),
            # resolved later by learning_loop
            "resolved_at":      None,
            "actual_temp_f":    None,
            "pl":               None,
        }
    )
    _save_json(_POSITIONS_FILE, positions)

    logger.info(
        "OrderManager: position opened – %s %s %d contracts @ %d¢  order=%s",
        side.upper(), ticker, contracts, price_cents, result["order_id"],
    )
    return {"success": True, "order_id": result["order_id"]}


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
