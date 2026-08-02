"""Depth-aware arbitrage calculation using executable buy-side asks."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from .config import kalshi_fee_per_contract, polymarket_fee_per_share, settings
from .execution import total_depth, walk_ask_levels
from .models import ArbitrageOpportunity, MatchedPair


def _quote_is_fresh(market, now: datetime) -> bool:
    received = market.quote_received_at
    if received is None:
        return False
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return 0 <= (now - received).total_seconds() <= settings.max_quote_age_seconds


def calculate_arbitrage(pair: MatchedPair) -> ArbitrageOpportunity:
    """Choose the cheaper fully hedged route using fresh depth-adjusted asks."""
    k = pair.kalshi
    p = pair.polymarket
    now = datetime.now(timezone.utc)
    if not (_quote_is_fresh(k, now) and _quote_is_fresh(p, now)):
        raise ValueError("missing or stale executable quote")

    p_yes_levels = p.no_asks if pair.inverted_outcomes else p.yes_asks
    p_no_levels = p.yes_asks if pair.inverted_outcomes else p.no_asks

    def route(k_levels, p_levels, k_side: str, p_side: str) -> dict | None:
        size = min(
            total_depth(k_levels),
            total_depth(p_levels),
            settings.quote_size_contracts,
        )
        if size < settings.min_executable_contracts:
            return None
        k_price, k_filled = walk_ask_levels(k_levels, size)
        p_price, p_filled = walk_ask_levels(p_levels, size)
        if k_price is None or p_price is None:
            return None
        k_fee = kalshi_fee_per_contract(
            k_price,
            maker=(settings.kalshi_fee_mode == "maker"),
            multiplier=k.fee_multiplier,
        )
        p_fee = polymarket_fee_per_share(
            p_price,
            category=p.category,
            fee_rate_override=p.fee_rate,
            fees_enabled=p.fees_enabled,
        )
        capital = k_price + p_price
        gross = 1.0 - capital
        return {
            "kalshi_side": k_side,
            "poly_side": p_side,
            "kalshi_price": k_price,
            "poly_price": p_price,
            "size": min(k_filled, p_filled),
            "kalshi_fee": k_fee,
            "poly_fee": p_fee,
            "capital": capital,
            "gross": gross,
            "net": gross - k_fee - p_fee - settings.execution_buffer_per_pair,
        }

    routes = [
        route(k.yes_asks, p_no_levels, "yes", "no"),
        route(k.no_asks, p_yes_levels, "no", "yes"),
    ]
    valid_routes = [candidate for candidate in routes if candidate is not None]
    if not valid_routes:
        raise ValueError("insufficient visible depth on one or both legs")
    best = max(valid_routes, key=lambda candidate: candidate["net"])

    buy_yes_on = "kalshi" if best["kalshi_side"] == "yes" else "polymarket"
    buy_no_on = "polymarket" if buy_yes_on == "kalshi" else "kalshi"
    total_fees = best["kalshi_fee"] + best["poly_fee"]
    roi_pct = (best["net"] / best["capital"] * 100.0) if best["capital"] > 0 else 0.0

    gap_cents = best["gross"] * 100.0
    suspicious = gap_cents > 10 or (gap_cents > 5 and pair.confidence < 90)

    def top(levels) -> float:
        return round(levels[0][0], 4) if levels else 0.0

    return ArbitrageOpportunity(
        event_name=k.event_title or p.event_title or k.title,
        kalshi_title=k.title,
        polymarket_title=p.title,
        kalshi_url=k.url,
        polymarket_url=p.url,
        match_confidence=pair.confidence,
        kalshi_market_id=k.market_id,
        polymarket_market_id=p.market_id,
        inverted_outcomes=pair.inverted_outcomes,
        kalshi_yes=top(k.yes_asks),
        kalshi_no=top(k.no_asks),
        polymarket_yes=top(p_yes_levels),
        polymarket_no=top(p_no_levels),
        buy_yes_on=buy_yes_on,
        buy_no_on=buy_no_on,
        kalshi_buy_side=best["kalshi_side"],
        polymarket_buy_side=best["poly_side"],
        kalshi_leg_price=round(best["kalshi_price"], 5),
        polymarket_leg_price=round(best["poly_price"], 5),
        kalshi_fee=round(best["kalshi_fee"], 5),
        polymarket_fee=round(best["poly_fee"], 5),
        total_fees=round(total_fees, 5),
        execution_buffer=round(settings.execution_buffer_per_pair, 5),
        gross_gap=round(best["gross"], 5),
        net_gap=round(best["net"], 5),
        capital_required=round(best["capital"], 5),
        roi_pct=round(roi_pct, 2),
        executable_size=round(best["size"], 4),
        kalshi_volume=k.volume,
        polymarket_volume=p.volume,
        category=p.category or k.category or "other",
        timestamp=now.isoformat(),
        match_reasons=pair.match_reasons,
        suspicious=suspicious,
        quote_valid=True,
        kalshi_quote_received_at=k.quote_received_at.isoformat(),
        polymarket_quote_received_at=p.quote_received_at.isoformat(),
    )


def calculate_all(pairs: List[MatchedPair], min_roi: float = 0.0) -> List[ArbitrageOpportunity]:
    """Calculate only pairs with complete fresh executable evidence."""
    opportunities: list[ArbitrageOpportunity] = []
    for pair in pairs:
        try:
            opportunity = calculate_arbitrage(pair)
        except ValueError:
            continue
        if opportunity.roi_pct >= min_roi:
            opportunities.append(opportunity)
    opportunities.sort(key=lambda opportunity: opportunity.roi_pct, reverse=True)
    return opportunities
