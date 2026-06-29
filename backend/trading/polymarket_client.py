"""
Polymarket data + execution client.

Read path (always available, no auth):
  - Gamma API  (https://gamma-api.polymarket.com)  → market discovery + metadata
  - CLOB API   (https://clob.polymarket.com)        → live prices + order book depth

Write path (gated behind POLYMARKET_LIVE_ENABLED + credentials):
  - py-clob-client → signed EIP-712 orders on Polygon

All Gamma list fields `outcomes`, `outcomePrices`, `clobTokenIds` come back as
JSON-encoded *strings* and must be json.loads()'d a second time. Outcome order
is NOT guaranteed, so we always map by outcome name, never by index.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.config import get_settings

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class Outcome:
    """One tradeable side of a market (e.g. 'Brazil', or 'Yes')."""
    name: str
    token_id: str
    price: float                 # last/implied price from Gamma (0–1)
    best_bid: float | None = None
    best_ask: float | None = None

    @property
    def mid_price(self) -> float:
        """Best available mid; falls back to Gamma price."""
        if self.best_bid is not None and self.best_ask is not None and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2
        return self.price


@dataclass
class PolyMarket:
    """A normalised Polymarket market with its tradeable outcomes."""
    market_id: str               # conditionId
    gamma_id: str
    slug: str
    question: str
    outcomes: list[Outcome] = field(default_factory=list)
    liquidity: float = 0.0
    volume_24h: float = 0.0
    end_date: str | None = None
    accepting_orders: bool = False
    closed: bool = False

    def outcome_by_name(self, name: str) -> Outcome | None:
        target = name.strip().lower()
        for o in self.outcomes:
            if o.name.strip().lower() == target:
                return o
        return None


class PolymarketClient:
    """Read market data from Gamma/CLOB; place orders via CLOB (live only)."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._clob = None  # lazy py-clob-client instance

    # ── Read: market discovery ────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> Any:
        r = await client.get(f"{GAMMA_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def fetch_markets(
        self,
        *,
        tag_slug: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> list[PolyMarket]:
        """
        Fetch active, open, order-accepting markets. Optionally filter by a tag
        slug (e.g. 'soccer', 'sports') or a free-text search term.
        """
        markets: list[PolyMarket] = []
        async with httpx.AsyncClient(timeout=20) as client:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            }
            if tag_slug:
                params["tag_slug"] = tag_slug

            try:
                if search:
                    data = await self._get(client, "/public-search", {"q": search})
                    raw_markets: list[Any] = []
                    if isinstance(data, dict):
                        raw_markets.extend(data.get("markets") or [])
                        # public-search nests match markets under events, not top-level
                        for evt in data.get("events") or []:
                            raw_markets.extend(evt.get("markets") or [])
                else:
                    raw_markets = await self._get(client, "/markets", params)
            except Exception as exc:
                logger.warning(f"Polymarket fetch_markets failed: {exc}")
                return []

            for raw in raw_markets or []:
                pm = self._parse_market(raw)
                if pm and pm.accepting_orders and not pm.closed:
                    markets.append(pm)

        logger.info(f"Polymarket: fetched {len(markets)} tradeable markets")
        return markets

    @staticmethod
    def _parse_market(raw: dict) -> PolyMarket | None:
        try:
            outcomes_raw = raw.get("outcomes", "[]")
            prices_raw = raw.get("outcomePrices", "[]")
            tokens_raw = raw.get("clobTokenIds", "[]")

            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw

            if not outcomes or len(outcomes) != len(prices) or len(outcomes) != len(tokens):
                return None

            best_bid = raw.get("bestBid")
            best_ask = raw.get("bestAsk")

            parsed_outcomes = []
            for i, name in enumerate(outcomes):
                parsed_outcomes.append(Outcome(
                    name=str(name),
                    token_id=str(tokens[i]),
                    price=float(prices[i]),
                    # bestBid/bestAsk on the market refer to the first (index-0)
                    # outcome; only attach to it to avoid mislabelling.
                    best_bid=float(best_bid) if (best_bid is not None and i == 0) else None,
                    best_ask=float(best_ask) if (best_ask is not None and i == 0) else None,
                ))

            return PolyMarket(
                market_id=str(raw.get("conditionId") or raw.get("condition_id") or raw.get("id", "")),
                gamma_id=str(raw.get("id", "")),
                slug=str(raw.get("slug", "")),
                question=str(raw.get("question", "")),
                outcomes=parsed_outcomes,
                liquidity=float(raw.get("liquidity") or 0),
                volume_24h=float(raw.get("volume24hr") or 0),
                end_date=raw.get("endDate"),
                accepting_orders=bool(raw.get("acceptingOrders", raw.get("enableOrderBook", False))),
                closed=bool(raw.get("closed", False)),
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            logger.debug(f"Could not parse Polymarket market: {exc}")
            return None

    # ── Read: live price + order book depth ───────────────────────────────────

    async def get_book_depth(self, token_id: str, side: str = "sell") -> tuple[float | None, float]:
        """
        Fetch the CLOB order book for a token and return:
          (best_price_for_taker, available_size_at_top_levels)

        side='sell' means the book's asks (what a BUYER would pay/take).
        Returns (None, 0.0) on failure — caller should treat as no liquidity.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
                r.raise_for_status()
                book = r.json()
        except Exception as exc:
            logger.debug(f"CLOB book fetch failed for {token_id}: {exc}")
            return None, 0.0

        # A buyer consumes the asks (lowest first)
        levels = book.get("asks") if side == "sell" else book.get("bids")
        if not levels:
            return None, 0.0

        # asks come sorted; aggregate the top 3 levels for a depth estimate
        try:
            sorted_levels = sorted(levels, key=lambda x: float(x["price"]),
                                   reverse=(side != "sell"))
            best_price = float(sorted_levels[0]["price"])
            depth = sum(float(l["size"]) * float(l["price"]) for l in sorted_levels[:3])
            return best_price, depth
        except (KeyError, ValueError, IndexError):
            return None, 0.0

    # ── Write: live order execution (gated) ───────────────────────────────────

    def _live_ready(self) -> tuple[bool, str]:
        s = self.settings
        if not s.polymarket_live_enabled:
            return False, "live trading disabled (POLYMARKET_LIVE_ENABLED=false)"
        if not s.polymarket_private_key:
            return False, "missing POLYMARKET_PRIVATE_KEY"
        if not (s.polymarket_api_key and s.polymarket_api_secret and s.polymarket_api_passphrase):
            return False, "missing CLOB API credentials"
        return True, ""

    def _get_clob_client(self):
        """Lazily construct a py-clob-client instance. Live mode only."""
        if self._clob is not None:
            return self._clob
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as exc:
            raise RuntimeError(
                "py-clob-client not installed. Run: pip install py-clob-client"
            ) from exc

        s = self.settings
        creds = ApiCreds(
            api_key=s.polymarket_api_key,
            api_secret=s.polymarket_api_secret,
            api_passphrase=s.polymarket_api_passphrase,
        )
        client = ClobClient(
            CLOB_BASE,
            key=s.polymarket_private_key,
            chain_id=137,  # Polygon mainnet
            creds=creds,
            funder=s.polymarket_funder_address or None,
        )
        self._clob = client
        return client

    def place_order(
        self,
        token_id: str,
        price: float,
        size_usdc: float,
    ) -> dict[str, Any]:
        """
        Place a live limit BUY order on the CLOB.

        Returns {"ok": bool, "order_id": str|None, "error": str|None}.
        This NEVER runs unless live mode is fully configured.
        """
        ready, reason = self._live_ready()
        if not ready:
            return {"ok": False, "order_id": None, "error": reason}

        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            client = self._get_clob_client()
            # Convert USDC notional → share size (shares = notional / price)
            shares = round(size_usdc / price, 2) if price > 0 else 0
            if shares <= 0:
                return {"ok": False, "order_id": None, "error": "non-positive size"}

            order_args = OrderArgs(
                price=round(price, 3),
                size=shares,
                side=BUY,
                token_id=token_id,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed)
            order_id = resp.get("orderID") or resp.get("orderId") if isinstance(resp, dict) else None
            logger.info(f"Polymarket LIVE order placed: token={token_id} ${size_usdc} → {order_id}")
            return {"ok": True, "order_id": order_id, "error": None}
        except Exception as exc:
            logger.error(f"Polymarket live order failed: {exc}")
            return {"ok": False, "order_id": None, "error": str(exc)}
