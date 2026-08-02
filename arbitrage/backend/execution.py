"""Executable order-book hydration for matched cross-venue markets."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Iterable

import httpx

from .config import settings
from .models import MatchedPair, NormalizedMarket, QuoteLevel

log = logging.getLogger(__name__)


def _levels(raw_levels, *, reverse_price: bool = False) -> list[QuoteLevel]:
    parsed: list[QuoteLevel] = []
    for level in raw_levels or []:
        try:
            if isinstance(level, dict):
                price = float(level["price"])
                size = float(level["size"])
            else:
                price = float(level[0])
                size = float(level[1])
            if reverse_price:
                price = 1.0 - price
            if 0.0 < price < 1.0 and size > 0:
                parsed.append((price, size))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return sorted(parsed, key=lambda item: item[0])


def parse_kalshi_asks(payload: dict) -> tuple[list[QuoteLevel], list[QuoteLevel]]:
    """Convert Kalshi YES/NO bid ladders into executable ask ladders."""
    book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    yes_bids = book.get("yes_dollars") or book.get("yes") or []
    no_bids = book.get("no_dollars") or book.get("no") or []
    return _levels(no_bids, reverse_price=True), _levels(yes_bids, reverse_price=True)


def parse_polymarket_asks(payload: dict) -> list[QuoteLevel]:
    return _levels(payload.get("asks") or [])


def walk_ask_levels(levels: list[QuoteLevel], target_size: float) -> tuple[float | None, float]:
    """Return (VWAP, filled size) while walking asks cheapest-first."""
    if target_size <= 0:
        return None, 0.0
    remaining = float(target_size)
    filled = 0.0
    cost = 0.0
    for price, available in sorted(levels, key=lambda item: item[0]):
        take = min(remaining, available)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return ((cost / filled) if filled else None), filled


def total_depth(levels: list[QuoteLevel]) -> float:
    return sum(size for _, size in levels)


async def _get_json(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    *,
    params: dict | None = None,
) -> tuple[dict | None, datetime]:
    async with semaphore:
        received_at = datetime.now(timezone.utc)
        try:
            response = await client.get(url, params=params, timeout=settings.request_timeout)
            response.raise_for_status()
            received_at = datetime.now(timezone.utc)
            data = response.json()
            return (data if isinstance(data, dict) else None), received_at
        except Exception as exc:
            log.debug("Order-book request failed for %s: %s", url, exc)
            return None, received_at


async def hydrate_executable_quotes(
    client: httpx.AsyncClient,
    pairs: Iterable[MatchedPair],
) -> None:
    """Populate ask ladders and Kalshi series fee multipliers for matches."""
    semaphore = asyncio.Semaphore(max(1, settings.book_concurrency))
    kalshi: dict[str, NormalizedMarket] = {}
    poly_tokens: dict[str, tuple[NormalizedMarket, str]] = {}
    series: dict[str, list[NormalizedMarket]] = {}

    for pair in pairs:
        km, pm = pair.kalshi, pair.polymarket
        kalshi[km.market_id] = km
        if km.series_ticker:
            series.setdefault(km.series_ticker, []).append(km)
        if pm.clob_token_id_yes:
            poly_tokens[pm.clob_token_id_yes] = (pm, "yes")
        if pm.clob_token_id_no:
            poly_tokens[pm.clob_token_id_no] = (pm, "no")

    kalshi_tasks = {
        ticker: asyncio.create_task(_get_json(
            client,
            semaphore,
            f"{settings.kalshi_base_url}/markets/{ticker}/orderbook",
        ))
        for ticker in kalshi
    }
    poly_tasks = {
        token: asyncio.create_task(_get_json(
            client,
            semaphore,
            f"{settings.polymarket_clob_url}/book",
            params={"token_id": token},
        ))
        for token in poly_tokens
    }
    series_tasks = {
        ticker: asyncio.create_task(_get_json(
            client,
            semaphore,
            f"{settings.kalshi_base_url}/series/{ticker}",
        ))
        for ticker in series
    }

    for ticker, task in kalshi_tasks.items():
        payload, received_at = await task
        market = kalshi[ticker]
        if payload:
            market.yes_asks, market.no_asks = parse_kalshi_asks(payload)
            market.quote_received_at = received_at
            market.quote_source = "kalshi_orderbook"

    for token, task in poly_tasks.items():
        payload, received_at = await task
        market, side = poly_tokens[token]
        if payload:
            setattr(market, f"{side}_asks", parse_polymarket_asks(payload))
            if market.quote_received_at is None or received_at > market.quote_received_at:
                market.quote_received_at = received_at
            market.quote_source = "polymarket_clob"

    for ticker, task in series_tasks.items():
        payload, _ = await task
        raw_series = (payload or {}).get("series") or {}
        try:
            multiplier = float(raw_series.get("fee_multiplier"))
        except (TypeError, ValueError):
            continue
        if multiplier > 0:
            for market in series[ticker]:
                market.fee_multiplier = multiplier
