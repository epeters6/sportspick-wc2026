"""Kalshi public API client — read-only, no auth required.

Uses the /events endpoint with nested markets to get real binary markets
with actual pricing data, avoiding MVE combo markets.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from .config import settings
from .models import NormalizedMarket

log = logging.getLogger(__name__)

# Category slug mapping — Kalshi uses internal category strings
_CATEGORY_MAP = {
    "economics": "economics",
    "politics": "politics",
    "finance": "finance",
    "crypto": "crypto",
    "climate": "weather",
    "weather": "weather",
    "sports": "sports",
    "entertainment": "culture",
    "tech": "tech",
    "science": "tech",
    "world": "geopolitics",
    "World": "geopolitics",
    "Elections": "politics",
    "Economics": "economics",
    "Financials": "finance",
    "Crypto": "crypto",
    "Climate and Weather": "weather",
    "Sports": "sports",
    "Culture": "culture",
    "Tech": "tech",
}


def _map_category(raw: str) -> str:
    """Map Kalshi's category string to our normalised category."""
    if not raw:
        return "other"
    return _CATEGORY_MAP.get(raw, _CATEGORY_MAP.get(raw.lower().strip(), "other"))


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _parse_price(market: dict) -> tuple[float, float]:
    """
    Extract YES/NO reference prices from a Kalshi market dict.

    Kalshi v2 returns dollar-denominated fields:
      - yes_bid_dollars / no_bid_dollars  (best bid)
      - yes_ask_dollars / no_ask_dollars  (best ask = what you'd pay)
      - last_price_dollars               (last traded price)

    Strategy:
      1. Compute midpoint of bid/ask for each side
      2. If bid=0 and ask>0, use the ask (thinly traded)
      3. Fall back to last_price_dollars if available
      4. In binary markets: no_price ≈ 1 - yes_price

    Returns (yes_price, no_price) in dollars (0.00 - 1.00).
    """
    def _float(val) -> float:
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    yb = _float(market.get("yes_bid_dollars"))
    ya = _float(market.get("yes_ask_dollars"))
    nb = _float(market.get("no_bid_dollars"))
    na = _float(market.get("no_ask_dollars"))
    lp = _float(market.get("last_price_dollars"))

    # Compute yes_price: midpoint if both sides exist, else whichever is available
    if yb > 0 and ya > 0:
        yes_price = (yb + ya) / 2.0
    elif ya > 0:
        yes_price = ya  # only ask available (thin market)
    elif yb > 0:
        yes_price = yb
    elif lp > 0:
        yes_price = lp  # last traded price as fallback
    else:
        # Try to infer from NO side: yes ≈ 1 - no
        if nb > 0 and na > 0:
            no_mid = (nb + na) / 2.0
            yes_price = 1.0 - no_mid
        elif nb > 0:
            yes_price = 1.0 - nb
        else:
            yes_price = 0.0

    # Compute no_price similarly
    if nb > 0 and na > 0:
        no_price = (nb + na) / 2.0
    elif na > 0:
        no_price = na
    elif nb > 0:
        no_price = nb
    else:
        no_price = 1.0 - yes_price if yes_price > 0 else 0.0

    return yes_price, no_price


async def fetch_kalshi_markets(client: httpx.AsyncClient) -> List[NormalizedMarket]:
    """
    Fetch active markets from Kalshi's public events API with nested markets.

    Uses /events?with_nested_markets=true to get individual binary markets
    with real prices, avoiding the MVE combo market flood.
    """
    base = settings.kalshi_base_url
    markets: List[NormalizedMarket] = []
    cursor: Optional[str] = None
    page = 0

    while page < settings.max_pages:
        params: dict = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            resp = await client.get(
                f"{base}/events",
                params=params,
                timeout=settings.request_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            log.warning("Kalshi events API error (page %d): %s", page, e)
            break
        except Exception as e:
            log.error("Kalshi events fetch failed (page %d): %s", page, e)
            break

        raw_events = data.get("events", [])
        if not raw_events:
            break

        for event in raw_events:
            event_title = event.get("title", "")
            event_ticker = event.get("event_ticker", "")
            event_category = event.get("category", "")
            category = _map_category(event_category)

            event_end = _parse_iso(
                event.get("expected_expiration_time")
                or event.get("close_time")
            )

            nested_markets = event.get("markets", [])
            if not nested_markets:
                continue

            for m in nested_markets:
                ticker = m.get("ticker", "")

                # Skip MVE combo legs
                mve_legs = m.get("mve_selected_legs")
                if isinstance(mve_legs, list) and len(mve_legs) > 0:
                    continue

                yes_price, no_price = _parse_price(m)
                if yes_price <= 0 and no_price <= 0:
                    continue

                # Volume: try volume_fp first, then volume_24h_fp
                vol_raw = m.get("volume_fp") or m.get("volume_24h_fp") or 0
                try:
                    volume = float(vol_raw)
                except (ValueError, TypeError):
                    volume = 0.0

                # Fee multiplier — some series override the default
                fee_mult = 1.0

                end_date = _parse_iso(
                    m.get("expiration_time")
                    or m.get("expected_expiration_time")
                    or m.get("close_time")
                ) or event_end

                title = m.get("title", "") or m.get("subtitle", "") or ticker
                # Strip markdown bold from titles
                title = title.replace("**", "")

                nm = NormalizedMarket(
                    platform="kalshi",
                    market_id=ticker,
                    title=title,
                    event_title=event_title,
                    category=category,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=volume,
                    end_date=end_date,
                    url=f"https://kalshi.com/markets/{ticker}" if ticker else None,
                    ticker=ticker,
                    event_ticker=event_ticker,
                    series_ticker=m.get("series_ticker") or event.get("series_ticker"),
                    fee_multiplier=fee_mult,
                    rules_primary=m.get("rules_primary"),
                    rules_secondary=m.get("rules_secondary"),
                )
                markets.append(nm)

        # Advance pagination
        cursor = data.get("cursor")
        if not cursor:
            break
        page += 1

    log.info("Fetched %d markets from Kalshi (%d event pages)", len(markets), page + 1)
    return markets
