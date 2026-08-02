"""Fail-closed settlement of verified paper arbitrage positions."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from .config import settings
from .database import get_open_shadow_bets, settle_shadow_bet

log = logging.getLogger(__name__)


def parse_kalshi_result(payload: dict) -> str | None:
    market = payload.get("market") or payload
    result = str(market.get("result") or "").strip().lower()
    return result if result in {"yes", "no"} else None


def _array(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            return []
    return []


def parse_polymarket_result(payload: dict) -> str | None:
    market = payload.get("market") or payload
    if not bool(market.get("closed")):
        return None
    outcomes = _array(market.get("outcomes"))
    prices = _array(market.get("outcomePrices"))
    if len(outcomes) != len(prices):
        return None
    winners = []
    for outcome, raw_price in zip(outcomes, prices):
        try:
            if float(raw_price) >= 0.999:
                winners.append(str(outcome).strip().lower())
        except (TypeError, ValueError):
            return None
    return winners[0] if len(winners) == 1 and winners[0] in {"yes", "no"} else None


async def settle_ready_shadow_bets(client: httpx.AsyncClient) -> int:
    """Resolve paper positions only when both venues publish final results."""
    settled = 0
    for bet in get_open_shadow_bets():
        try:
            kalshi_req = client.get(
                f"{settings.kalshi_base_url}/markets/{bet['kalshi_market_id']}",
                timeout=settings.request_timeout,
            )
            poly_req = client.get(
                f"{settings.polymarket_gamma_url}/markets/{bet['polymarket_market_id']}",
                timeout=settings.request_timeout,
            )
            kalshi_response, poly_response = await asyncio.gather(kalshi_req, poly_req)
            kalshi_response.raise_for_status()
            poly_response.raise_for_status()
            kalshi_result = parse_kalshi_result(kalshi_response.json())
            polymarket_result = parse_polymarket_result(poly_response.json())
            if kalshi_result and polymarket_result and settle_shadow_bet(
                int(bet["id"]), kalshi_result, polymarket_result
            ):
                settled += 1
        except Exception as exc:
            log.debug("Settlement check failed for paper bet %s: %s", bet.get("id"), exc)
    return settled
