"""Data models shared across the arbitrage scanner."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

QuoteLevel = tuple[float, float]


@dataclass
class NormalizedMarket:
    """Platform-neutral binary market plus executable quote evidence."""

    platform: str
    market_id: str
    title: str
    event_title: str
    category: str
    yes_price: float
    no_price: float
    volume: float
    end_date: Optional[datetime] = None
    url: Optional[str] = None

    ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    series_ticker: Optional[str] = None
    fee_multiplier: float = 1.0

    clob_token_id_yes: Optional[str] = None
    clob_token_id_no: Optional[str] = None
    slug: Optional[str] = None
    condition_id: Optional[str] = None
    fee_rate: Optional[float] = None
    fees_enabled: bool = True

    # Discovery prices above are never treated as fills. These ladders are
    # populated from venue order books after a strict semantic match.
    yes_asks: list[QuoteLevel] = field(default_factory=list)
    no_asks: list[QuoteLevel] = field(default_factory=list)
    quote_received_at: Optional[datetime] = None
    quote_source: Optional[str] = None

    rules_primary: Optional[str] = None
    rules_secondary: Optional[str] = None
    resolution_source: Optional[str] = None

    _normalised: str = ""
    _anchors: set[str] = field(default_factory=set, repr=False)


@dataclass
class MatchedPair:
    """Two venue markets that passed hard settlement-identity checks."""

    kalshi: NormalizedMarket
    polymarket: NormalizedMarket
    confidence: float
    inverted_outcomes: bool = False
    match_reasons: list[str] = field(default_factory=list)


@dataclass
class ArbitrageOpportunity:
    """Fee- and depth-adjusted opportunity calculated from executable asks."""

    event_name: str
    kalshi_title: str
    polymarket_title: str
    kalshi_url: Optional[str]
    polymarket_url: Optional[str]
    match_confidence: float
    kalshi_market_id: str
    polymarket_market_id: str
    inverted_outcomes: bool

    kalshi_yes: float
    kalshi_no: float
    polymarket_yes: float
    polymarket_no: float

    buy_yes_on: str
    buy_no_on: str
    kalshi_buy_side: str
    polymarket_buy_side: str
    kalshi_leg_price: float
    polymarket_leg_price: float

    kalshi_fee: float
    polymarket_fee: float
    total_fees: float
    execution_buffer: float

    gross_gap: float
    net_gap: float
    capital_required: float
    roi_pct: float
    executable_size: float

    kalshi_volume: float
    polymarket_volume: float
    category: str

    timestamp: str = ""
    match_reasons: list[str] = field(default_factory=list)
    suspicious: bool = False
    quote_valid: bool = False
    quote_source: str = "orderbook_v2"
    kalshi_quote_received_at: Optional[str] = None
    polymarket_quote_received_at: Optional[str] = None

    @property
    def is_profitable(self) -> bool:
        return self.quote_valid and not self.suspicious and self.net_gap > 0
