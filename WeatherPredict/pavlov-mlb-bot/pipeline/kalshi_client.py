"""
Kalshi client stub for pavlov-mlb-bot.

The copied ``discord_bot`` still exposes Kalshi-oriented slash commands; this module
avoids hard failures when Kalshi credentials are not configured (MLB primary path is Polymarket).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def format_open_position_embed_field(
    ticker: str,
    side: str,
    n_contracts: float,
    exposure_dollars,
) -> tuple[str, str]:
    title = (ticker or "?")[:80]
    try:
        exp_f = float(exposure_dollars)
    except (TypeError, ValueError):
        exp_f = 0.0
    value = f"**{side}** {n_contracts:g}× · ${exp_f:,.2f}\n`{ticker}`"
    return title, value


def get_account_balance() -> float:
    logger.debug("Kalshi stub: get_account_balance → 0 (MLB bot is Polymarket-first)")
    return 0.0


def market_position_net_contracts(p: dict) -> float:
    raw = p.get("position", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def get_open_positions() -> list[dict]:
    return []


def get_market_as_parsed(ticker: str) -> dict | None:
    return None


def place_order(
    ticker: str,
    side: str,
    contracts: int,
    price_cents: int,
) -> dict[str, Any]:
    return {
        "order_id": "",
        "status": "error",
        "filled_contracts": 0,
        "price": price_cents,
        "error": "Kalshi not configured on pavlov-mlb-bot (stub client)",
    }


def get_market_result(ticker: str) -> str | None:
    return None


def get_weather_markets() -> list[dict]:
    return []
