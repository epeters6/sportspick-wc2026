"""Conservative paper fees for Kalshi and this project's Polymarket US adapter.

Sources checked September 6, 2026:
https://docs.polymarket.us/fees (effective July 1, 2026)
https://kalshi.com/docs/kalshi-fee-schedule.pdf (July 7, 2026)
https://docs.kalshi.com/getting_started/fee_rounding

``polymarket`` is the existing US adapter alias; international CLOB fees are
deliberately unsupported. Actual execution commissions always supersede these
paper estimates. No account-tier or maker rebates are credited.
"""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_EVEN
from typing import Literal, Optional

FEE_MODEL_VERSION = "weather_fee_2026_09_v1"
POLYMARKET_US_FEE_SOURCE = "https://docs.polymarket.us/fees"
KALSHI_FEE_SOURCE = "https://docs.kalshi.com/getting_started/fee_rounding"
_CENT = Decimal("0.01")
_SIX_DP = Decimal("0.000001")


def _decimal(value, name: str, *, positive: bool = False) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (positive and number == 0):
            raise ValueError
        return number
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"INVALID_FEE_{name.upper()}") from None


def estimate_trade_fee(
    platform: str, price: float, shares: float, *,
    liquidity_role: Literal["maker", "taker"] = "taker",
    fee_type: str | None = None, fee_multiplier: float | None = None,
    balance_precision: float = 0.01, api_fee_override: float | None = None,
    conservative: bool = False, require_verified_schedule: bool = False,
    fee_source: str | None = None,
) -> dict:
    """Return total fee, per-share budget, and the precise model assumptions.

    Default calculates one fill/order at one price. Conservative mode budgets
    separately rounded whole contracts, so partial *whole-contract* fills cannot
    exceed the budget. Weather paper sizing must round depth down to whole units.
    This over-reserves fees; it is not an assertion that exchanges charge each
    contract separately. Kalshi defaults to non-direct cent balance precision.
    """
    venue = str(platform).lower()
    if venue not in {"kalshi", "polymarket", "polymarket_us"}:
        raise ValueError(f"FEE_MODEL_UNAVAILABLE: {platform}")
    if liquidity_role not in {"maker", "taker"}:
        raise ValueError("INVALID_FEE_LIQUIDITY_ROLE")
    p = _decimal(price, "price", positive=True)
    quantity = _decimal(shares, "shares", positive=True)
    if p >= 1:
        raise ValueError("INVALID_FEE_PRICE")
    if conservative and quantity != quantity.to_integral_value():
        raise ValueError("FRACTIONAL_FEE_BUDGET_UNSUPPORTED")
    multiplier = _decimal(1 if fee_multiplier is None else fee_multiplier, "multiplier")
    precision = _decimal(balance_precision, "balance_precision", positive=True)
    if precision not in {_CENT, Decimal("0.0001")}:
        raise ValueError("INVALID_FEE_BALANCE_PRECISION")
    if venue == "kalshi":
        if require_verified_schedule and (fee_type is None or fee_multiplier is None or not fee_source):
            raise ValueError("KALSHI_FEE_SCHEDULE_UNVERIFIED")
        kind = fee_type or "quadratic"
        if kind not in {"quadratic", "quadratic_with_maker_fees"}:
            raise ValueError(f"FEE_MODEL_UNAVAILABLE: Kalshi {kind}")
        coefficient = Decimal("0.07") if liquidity_role == "taker" else (
            Decimal("0.0175") if kind == "quadratic_with_maker_fees" else Decimal(0)
        )
        source = fee_source or "kalshi_general_schedule_assumption"
    else:
        if fee_type not in (None, "quadratic") or multiplier != 1:
            raise ValueError("UNSUPPORTED_POLYMARKET_US_FEE_OVERRIDE")
        kind = "quadratic"
        coefficient = Decimal("0.06") if liquidity_role == "taker" else Decimal(0)
        source = fee_source or POLYMARKET_US_FEE_SOURCE
    exact_per_share = coefficient * multiplier * p * (1 - p)
    if api_fee_override is not None:
        exact_per_share = _decimal(api_fee_override, "override")
        if exact_per_share >= 1 - p:
            raise ValueError("INVALID_FEE_OVERRIDE")
        source = "explicit_per_share_override"

    def rounded_fee(count: Decimal) -> Decimal:
        exact = exact_per_share * count
        if venue != "kalshi":
            # The US venue caps cumulative taker fees at this banker-rounded
            # cumulative exact fee; fragmented matches cannot increase that cap.
            return exact.quantize(_CENT, rounding=ROUND_HALF_EVEN)
        trade_fee = exact.quantize(_SIX_DP, rounding=ROUND_CEILING)
        cost = p * count
        # Buyer revenue is negative. Align the debit down to the precision grid;
        # equivalently round cost + trade fee up and subtract the unrounded cost.
        debit = ((cost + trade_fee) / precision).to_integral_value(rounding=ROUND_CEILING) * precision
        return debit - cost

    if conservative:
        if venue == "kalshi":
            unit_fee = rounded_fee(Decimal(1))
        else:
            # A one-contract banker-rounded fee can be zero while a larger
            # cumulative order is charged. Ceiling avoids this underbudgeting.
            unit_fee = exact_per_share.quantize(_CENT, rounding=ROUND_CEILING)
        total = unit_fee * quantity
    else:
        total = rounded_fee(quantity)
    return {
        "fee_model_version": FEE_MODEL_VERSION,
        "fee_venue_product": "kalshi" if venue == "kalshi" else "polymarket_us",
        "fee_source": source, "fee_type": kind, "fee_multiplier": float(multiplier),
        "fee_coefficient": float(coefficient), "fee_per_share": float(total / quantity),
        "fee_total": float(total), "fee_exact_unrounded": float(exact_per_share * quantity),
        "fee_rounding_policy": "whole_contract_conservative_budget" if conservative else (
            "kalshi_buy_balance_alignment" if venue == "kalshi" else "us_bankers_order_cap"
        ),
        "fee_balance_precision": float(precision) if venue == "kalshi" else 0.01,
        "fee_rebates_credited": False,
    }

def estimate_fee_per_share(
    platform: str,
    price: float,
    shares: float,
    market_type: Optional[str] = None,
    liquidity_role: Literal["maker", "taker"] = "taker",
    api_fee_override: Optional[float] = None,
    **kwargs,
) -> float:
    """Backward-compatible conservative quote for existing US/Kalshi callers."""
    if market_type in {"international", "clob", "polymarket_international"}:
        raise ValueError("FEE_MODEL_UNAVAILABLE: international Polymarket")
    return estimate_trade_fee(
        platform, price, shares, liquidity_role=liquidity_role,
        api_fee_override=api_fee_override, conservative=True, **kwargs,
    )["fee_per_share"]


def weather_fee_metadata(platform: str, price: float, market: dict) -> dict:
    """One-contract budget and provenance to persist in weather decisions/fills."""
    if market.get("fee_error"):
        raise ValueError(str(market["fee_error"]))
    result = estimate_trade_fee(
        platform, price, 1, conservative=True, require_verified_schedule=True,
        fee_type=market.get("fee_type"), fee_multiplier=market.get("fee_multiplier"),
        fee_source=market.get("fee_source"),
    )
    result["fee_schedule_checked_at"] = market.get("fee_schedule_checked_at")
    result["fee_schedule_max_age_seconds"] = market.get("fee_schedule_max_age_seconds")
    return result
