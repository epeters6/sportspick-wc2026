"""Polymarket public API client — Gamma (metadata) + CLOB (prices)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from .config import settings, POLYMARKET_TAKER_RATES
from .models import NormalizedMarket

log = logging.getLogger(__name__)

# Known Polymarket tag slugs → our category names
_SLUG_TO_CATEGORY = {
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "bitcoin": "crypto",
    "ethereum": "crypto",
    "sports": "sports",
    "nfl": "sports",
    "nba": "sports",
    "mlb": "sports",
    "soccer": "sports",
    "football": "sports",
    "baseball": "sports",
    "basketball": "sports",
    "mma": "sports",
    "ufc": "sports",
    "tennis": "sports",
    "hockey": "sports",
    "finance": "finance",
    "stocks": "finance",
    "politics": "politics",
    "elections": "politics",
    "election": "politics",
    "economics": "economics",
    "economy": "economics",
    "fed": "economics",
    "inflation": "economics",
    "gdp": "economics",
    "jobs": "economics",
    "culture": "culture",
    "entertainment": "culture",
    "weather": "weather",
    "climate": "weather",
    "tech": "tech",
    "technology": "tech",
    "ai": "tech",
    "geopolitics": "geopolitics",
    "world": "geopolitics",
    "war": "geopolitics",
    "mentions": "mentions",
}


def _guess_category(event: dict, market: dict) -> str:
    """
    Best-effort category assignment from event/market metadata.

    Checks event category field, tags, slugs, and title keywords.
    """
    # Check event-level category field first (most reliable)
    raw_cat = (event.get("category") or "").lower().strip()
    if raw_cat:
        mapped = _SLUG_TO_CATEGORY.get(raw_cat, raw_cat)
        if mapped in POLYMARKET_TAKER_RATES:
            return mapped

    # Check feeType field (e.g. "sports_fees_v2" → "sports")
    fee_type = (market.get("feeType") or "").lower()
    for key in _SLUG_TO_CATEGORY:
        if key in fee_type:
            return _SLUG_TO_CATEGORY[key]

    # Check tags if present
    for tag_field in ("tags", "tag_slugs"):
        tags = event.get(tag_field) or market.get(tag_field) or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = [tags]
        for tag in tags:
            if isinstance(tag, dict):
                tag = tag.get("slug", "") or tag.get("label", "")
            cat = _SLUG_TO_CATEGORY.get(str(tag).lower().strip())
            if cat:
                return cat

    # Check slug
    slug = (market.get("slug") or event.get("slug") or "").lower()
    for keyword, cat in _SLUG_TO_CATEGORY.items():
        if keyword in slug:
            return cat

    # Check title keywords
    title = (market.get("question") or event.get("title") or "").lower()
    for keyword, cat in _SLUG_TO_CATEGORY.items():
        if keyword in title:
            return cat

    return "other"


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _safe_json_loads(val) -> list:
    """Parse a stringified JSON array (Polymarket's format) or return as-is."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _extract_fee_rate(market: dict) -> tuple[Optional[float], bool]:
    """
    Extract the actual fee rate from a Polymarket market's feeSchedule.

    Returns (fee_rate, fees_enabled).
    The feeSchedule field looks like:
        {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15}
    """
    fees_enabled = market.get("feesEnabled", True)
    if not fees_enabled:
        return 0.0, False

    schedule = market.get("feeSchedule")
    if isinstance(schedule, dict):
        rate = schedule.get("rate")
        if rate is not None:
            try:
                return float(rate), True
            except (ValueError, TypeError):
                pass

    return None, fees_enabled


async def fetch_polymarket_markets(client: httpx.AsyncClient) -> List[NormalizedMarket]:
    """
    Fetch all active markets from Polymarket's Gamma API.

    Events endpoint embeds markets, so we get both in one pass.
    Uses 'volume24hr' sort (no underscore — confirmed against live API).
    """
    base = settings.polymarket_gamma_url
    markets: List[NormalizedMarket] = []
    offset = 0
    limit = 100
    page = 0

    while page < settings.max_pages:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",      # no underscore — API rejects "volume_24hr"
            "ascending": "false",
        }

        try:
            resp = await client.get(
                f"{base}/events",
                params=params,
                timeout=settings.request_timeout,
            )
            resp.raise_for_status()
            events = resp.json()
        except httpx.HTTPStatusError as e:
            log.warning("Polymarket Gamma API error (page %d): %s", page, e)
            break
        except Exception as e:
            log.error("Polymarket Gamma fetch failed (page %d): %s", page, e)
            break

        if not events:
            break

        for event in events:
            event_title = event.get("title", "")
            event_end = _parse_iso(event.get("endDate"))
            event_slug = event.get("slug", "")

            embedded_markets = event.get("markets", [])
            if not embedded_markets:
                continue

            for mkt in embedded_markets:
                # Parse stringified arrays
                outcomes = _safe_json_loads(mkt.get("outcomes"))
                prices = _safe_json_loads(mkt.get("outcomePrices"))
                token_ids = _safe_json_loads(mkt.get("clobTokenIds"))

                # Need at least YES/NO outcomes with prices
                if len(outcomes) < 2 or len(prices) != len(outcomes) or len(token_ids) != len(outcomes):
                    continue

                # Map outcomes to YES/NO by inspecting the outcomes array
                # Polymarket can return outcomes in any order: ["Yes","No"] or ["No","Yes"]
                # For non-binary markets (e.g. ["Trump","Biden"]), treat index 0 as YES
                outcomes_lower = [o.lower().strip() for o in outcomes]
                try:
                    if "yes" in outcomes_lower and "no" in outcomes_lower:
                        yes_idx = outcomes_lower.index("yes")
                        no_idx = outcomes_lower.index("no")
                    else:
                        # This scanner hedges binary YES/NO contracts only.
                        continue
                    yes_price = float(prices[yes_idx])
                    no_price = float(prices[no_idx])
                except (ValueError, TypeError, IndexError):
                    continue

                if yes_price <= 0 and no_price <= 0:
                    continue

                # Extract per-market fee rate from feeSchedule
                fee_rate, fees_enabled = _extract_fee_rate(mkt)

                # Determine category for fee calculation (fallback if no feeSchedule)
                category = _guess_category(event, mkt)

                # Volume
                vol = 0.0
                for vol_field in ("volume24hr", "volume"):
                    v = mkt.get(vol_field)
                    if v is not None:
                        try:
                            vol = float(v)
                            if vol > 0:
                                break
                        except (ValueError, TypeError):
                            pass

                # Token IDs for CLOB price lookups (respect same index mapping)
                token_yes = str(token_ids[yes_idx])
                token_no = str(token_ids[no_idx])

                market_id = mkt.get("id", "") or mkt.get("conditionId", "")
                question = mkt.get("question", "") or mkt.get("title", "")
                slug = mkt.get("slug", "") or event_slug

                end_date = _parse_iso(mkt.get("endDate")) or event_end

                nm = NormalizedMarket(
                    platform="polymarket",
                    market_id=str(market_id),
                    title=question,
                    event_title=event_title,
                    category=category,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=vol,
                    end_date=end_date,
                    url=f"https://polymarket.com/event/{event_slug}" if event_slug else None,
                    slug=slug,
                    condition_id=mkt.get("conditionId"),
                    clob_token_id_yes=token_yes,
                    clob_token_id_no=token_no,
                    fee_rate=fee_rate,
                    fees_enabled=fees_enabled,
                    resolution_source=(
                        mkt.get("resolutionSource")
                        or mkt.get("resolution_source")
                        or event.get("resolutionSource")
                    ),
                )
                markets.append(nm)

        offset += limit
        page += 1

        # If fewer results than limit, we've reached the end
        if len(events) < limit:
            break

    log.info("Fetched %d active markets from Polymarket (%d pages)", len(markets), page + 1)
    return markets
