"""Order placement + skip logging for Polymarket US (isolated from Kalshi)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone

try:
    from pavlov.config import CONFIG
    from pavlov.pipeline import signal_learning_log
    from pavlov.pipeline.execution_ledger import begin_attempt, complete_attempt
    from pavlov.polymarket import paths as poly_paths, poly_client
except ModuleNotFoundError as exc:
    if exc.name != "pavlov":
        raise
    from config import CONFIG
    from pipeline import signal_learning_log
    from pipeline.execution_ledger import begin_attempt, complete_attempt
    from polymarket import paths as poly_paths, poly_client

logger = logging.getLogger(__name__)


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


async def place_trade(signal: dict) -> dict:
    """Place a limit order on Polymarket US."""
    slug      = signal["ticker"]
    side      = signal["recommended_side"]
    contracts = int(signal["kelly_contracts"])

    # YES: limit price ≈ cost per $1 payoff share of YES.
    # NO: unit here is cost of NO (1 − YES implied); Polymarket ``BUY_SHORT`` uses the
    # complementary YES price inside ``poly_client.place_order``.
    raw_price = (
        signal["implied_prob"] if side == "yes"
        else (1 - signal["implied_prob"])
    )
    price_prob = max(0.01, min(0.99, float(raw_price)))

    # Whole-dollar notional floor: est. spend = qty × unit; round up to \$1 steps (min POLY_MIN_NOTIONAL_USD).
    min_usd = float(CONFIG.get("POLY_MIN_NOTIONAL_USD") or 0.0)
    if min_usd > 0 and contracts >= 1:
        raw_stake_usd = contracts * price_prob
        target_usd = max(min_usd, math.ceil(raw_stake_usd - 1e-9))
        need = max(contracts, int(math.ceil(target_usd / price_prob - 1e-9)))
        if need > contracts:
            logger.info(
                "PolyOrderManager: %s qty %d → %d (est. ~$%.2f → ceil whole USD $%.0f; unit=%.3f)",
                slug,
                contracts,
                need,
                raw_stake_usd,
                target_usd,
                price_prob,
            )
            contracts = need

    try:
        attempt_id = begin_attempt(
            poly_paths.POSITIONS, signal, venue="poly_us", quantity=float(contracts),
            price=min(0.99, price_prob + 0.01),
        )
    except (OSError, ValueError) as exc:
        return {"success": False, "error": str(exc)}

    try:
        result = await asyncio.to_thread(
            poly_client.place_order,
            slug,
            side,
            contracts,
            price_prob,
        )
    except Exception as exc:
        logger.error("PolyOrderManager: place_order raised – %s", exc)
        result = {"status": "submission_unknown", "error": str(exc)}
    return complete_attempt(poly_paths.POSITIONS, attempt_id, result)


def log_skip(signal: dict) -> None:
    """Append a skip record to logs_poly/signals.json."""
    if signal_learning_log.append_learning_record(
        poly_paths.SIGNALS,
        signal,
        action="skip",
        learn_reason="discord_skip",
        learn_source="discord",
        venue="poly_us",
    ):
        logger.info("PolyOrderManager: skip logged for %s.", signal.get("ticker"))


def log_signal_watch(signal: dict, reason: str) -> None:
    """Log a Poly signal that did not become a position (e.g. auto-bet rejected)."""
    if signal_learning_log.append_learning_record(
        poly_paths.SIGNALS,
        signal,
        action="signal_watch",
        learn_reason=(reason or "").strip(),
        learn_source="auto",
        venue="poly_us",
    ):
        logger.info(
            "PolyOrderManager: signal_watch logged for %s — %s",
            signal.get("ticker"),
            (reason or "")[:160],
        )
