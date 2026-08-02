"""
Configuration for the Kalshi ↔ Polymarket Arbitrage Scanner.

Fee models use the exact formulas from each platform's official docs:
  Kalshi:      fee = ceil(M × rate × C × P × (1-P))   (to nearest $0.0001)
  Polymarket:  fee = C × rate × P × (1-P)              (rounded to 5 decimals)

Both are price-dependent: highest at P=0.50, falling to near-zero at extremes.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict


# ---------------------------------------------------------------------------
# Kalshi fee schedule
# ---------------------------------------------------------------------------
# Source: https://kalshi.com/fee-schedule  &  docs.kalshi.com/getting_started/fee_rounding
# Formula:  fee_per_contract = ceil_centicent(multiplier × rate × P × (1-P))
#   - Taker rate = 0.07
#   - Maker rate = 0.0175
#   - Multiplier M = 1 for most markets (some series override this)
#   - "ceil_centicent" = round UP to nearest $0.0001
#
# For arbitrage scanning we assume *taker* execution (worst-case).

KALSHI_TAKER_RATE: float = 0.07
KALSHI_MAKER_RATE: float = 0.0175
KALSHI_DEFAULT_MULTIPLIER: float = 1.0


def kalshi_fee_per_contract(price_dollars: float,
                            *,
                            maker: bool = False,
                            multiplier: float = KALSHI_DEFAULT_MULTIPLIER) -> float:
    """
    Kalshi fee for ONE contract at the given price (in dollars, 0-1).

    Returns fee in **dollars** (e.g. 0.0175 = 1.75¢).
    Uses ceil-to-centicent rounding per Kalshi's docs.
    """
    rate = KALSHI_MAKER_RATE if maker else KALSHI_TAKER_RATE
    raw = multiplier * rate * price_dollars * (1.0 - price_dollars)
    # Round UP to nearest $0.0001 (centicent)
    return math.ceil(raw * 10_000) / 10_000


# ---------------------------------------------------------------------------
# Polymarket fee schedule
# ---------------------------------------------------------------------------
# Source: https://docs.polymarket.com/trading/fees
# Formula:  fee_per_share = rate × P × (1 - P)
#   - Makers are NEVER charged fees.
#   - Taker rates vary by market category.
#   - Geopolitics markets are fee-free.
#   - Rounded to 5 decimal places.

POLYMARKET_TAKER_RATES: Dict[str, float] = {
    "crypto":     0.07,
    "sports":     0.03,
    "finance":    0.04,
    "politics":   0.04,
    "economics":  0.05,
    "culture":    0.05,
    "weather":    0.05,
    "other":      0.05,
    "general":    0.05,
    "mentions":   0.04,
    "tech":       0.04,
    "geopolitics": 0.0,   # fee-free
}

POLYMARKET_DEFAULT_TAKER_RATE: float = 0.05   # fallback if category unknown


def polymarket_fee_per_share(price: float,
                             category: str = "other",
                             fee_rate_override: float | None = None,
                             fees_enabled: bool = True) -> float:
    """
    Polymarket taker fee for ONE share at the given price (0-1).

    Returns fee in **dollars**. Makers pay zero.
    If fee_rate_override is provided (from the market's feeSchedule),
    it takes priority over the category-based lookup.
    """
    if not fees_enabled:
        return 0.0

    if fee_rate_override is not None:
        rate = fee_rate_override
    else:
        rate = POLYMARKET_TAKER_RATES.get(category.lower().strip(),
                                           POLYMARKET_DEFAULT_TAKER_RATE)
    raw = rate * price * (1.0 - price)
    return round(raw, 5)


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    """Runtime-configurable settings, loadable from env vars."""

    # How often to refresh data (seconds)
    refresh_interval: int = int(os.getenv("ARB_REFRESH_INTERVAL", "30"))

    # Matching thresholds (0-100)
    match_auto_threshold: float = float(os.getenv("ARB_MATCH_AUTO", "85"))
    match_review_threshold: float = float(os.getenv("ARB_MATCH_REVIEW", "70"))

    # Minimum ROI% to display (0 = show all)
    min_roi_pct: float = float(os.getenv("ARB_MIN_ROI", "0"))

    # Fee mode: "taker" (conservative/default) or "maker" (optimistic)
    kalshi_fee_mode: str = os.getenv("ARB_KALSHI_FEE_MODE", "taker")
    kalshi_multiplier: float = float(os.getenv("ARB_KALSHI_MULTIPLIER", "1.0"))

    # Polymarket default category (used when category can't be determined)
    polymarket_default_category: str = os.getenv("ARB_PM_CATEGORY", "other")

    # API base URLs
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"

    # Request timeouts (seconds)
    request_timeout: float = float(os.getenv("ARB_TIMEOUT", "15"))

    # Maximum pages to fetch from each platform per cycle
    max_pages: int = int(os.getenv("ARB_MAX_PAGES", "10"))

    # Execution evidence: fail closed without fresh, visible depth.
    quote_size_contracts: float = float(os.getenv("ARB_QUOTE_SIZE", "25"))
    min_executable_contracts: float = float(os.getenv("ARB_MIN_EXECUTABLE_SIZE", "1"))
    max_quote_age_seconds: float = float(os.getenv("ARB_MAX_QUOTE_AGE", "20"))
    book_concurrency: int = int(os.getenv("ARB_BOOK_CONCURRENCY", "12"))
    execution_buffer_per_pair: float = float(os.getenv("ARB_EXECUTION_BUFFER", "0.01"))

    # Paper and history controls.
    shadow_max_contracts: int = int(os.getenv("ARB_SHADOW_MAX_CONTRACTS", "25"))
    shadow_bankroll: float = float(os.getenv("ARB_SHADOW_BANKROLL", "10000"))
    history_retention_days: int = int(os.getenv("ARB_HISTORY_RETENTION_DAYS", "14"))

    # Mutating API routes are disabled unless this token is set.
    admin_token: str = os.getenv("ARB_ADMIN_TOKEN", "")
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "ARB_CORS_ORIGINS",
            "http://127.0.0.1:8000,http://localhost:8000",
        ).split(",")
        if origin.strip()
    )

    # Server
    host: str = os.getenv("ARB_HOST", "127.0.0.1")
    port: int = int(os.getenv("ARB_PORT", "8000"))


settings = Settings()
