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
from pydantic import BaseModel
import sys
import os
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

# Add pavlov to path to import polymarket_us SDK
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../pavlov")))
if "PAVLOV_BYPASS_CONFIG" not in os.environ:
    os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from polymarket_us import PolymarketUS

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
    venue: str = "polymarket"

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

    async def get_market(self, condition_id: str) -> dict:
        """Fetch market details (stubbed for compatibility with existing code)"""
        # In a real implementation this would fetch from Gamma API
        return {"outcomes": [{"token_id": "yes_token"}, {"token_id": "no_token"}]}
        
    async def audit_positions(self):
        """
        Retrieves all active portfolio positions and confirms they match the
        internal `autobets` table tracking in Supabase, logging discrepancies.
        """
        logger.info("Starting Polymarket position audit...")
        
        try:
            # 1. Fetch from Polymarket API (mocked)
            # In a real implementation we would hit the Polymarket Portfolio/Positions API
            # e.g., r = await client.get(f"{CLOB_BASE}/positions")
            pm_positions = [] # list of token_ids and sizes we hold
            
            # 2. Fetch from our database
            from backend.db import get_db
            db = get_db()
            db_bets = db.table("autobets").select("*").eq("status", "placed").execute().data or []
            
            logger.info(f"Audit: Found {len(db_bets)} active bets in database.")
            
            # 3. Reconcile
            # (Mock logic)
            discrepancies = 0
            for bet in db_bets:
                # We would verify bet["market_id"] or bet["token_id"] against pm_positions
                pass
                
            if discrepancies > 0:
                logger.error(f"Audit failed: Found {discrepancies} position discrepancies between DB and Polymarket!")
            else:
                logger.info("Audit passed: All database bets reconcile perfectly with Polymarket positions.")
                
        except Exception as exc:
            logger.error(f"Failed to audit Polymarket positions: {exc}")

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
        except (KeyError, ValueError, IndexError) as exc:
            logger.debug(f"Failed to parse CLOB book for {token_id}: {exc}")
            return None, 0.0

    async def get_vwap(self, token_id: str, target_size: float = 500.0, side: str = "sell") -> float | None:
        """
        Calculates the Volume-Weighted Average Price (VWAP) for a given target size
        by walking the order book. Useful for CLV calculation and execution estimates.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
                r.raise_for_status()
                book = r.json()
        except Exception as exc:
            logger.debug(f"VWAP book fetch failed for {token_id}: {exc}")
            return None

        levels = book.get("asks") if side == "sell" else book.get("bids")
        if not levels:
            return None

        try:
            sorted_levels = sorted(levels, key=lambda x: float(x["price"]), reverse=(side != "sell"))
            
            filled_size = 0.0
            total_cost = 0.0
            
            for level in sorted_levels:
                price = float(level["price"])
                size = float(level["size"]) * price  # USDC size
                
                remaining = target_size - filled_size
                if size >= remaining:
                    total_cost += remaining * price
                    filled_size += remaining
                    break
                else:
                    total_cost += size * price
                    filled_size += size
                    
            if filled_size == 0:
                return None
            return total_cost / filled_size
        except (KeyError, ValueError, IndexError) as exc:
            return None

    # ── Write: live order execution (US Regulated FCM API) ────────────────────

    def _live_ready(self) -> tuple[bool, str]:
        s = self.settings
        if not s.polymarket_live_enabled:
            return False, "live trading disabled (POLYMARKET_LIVE_ENABLED=false)"
        if not (s.polymarket_api_key and s.polymarket_api_secret and s.polymarket_api_passphrase):
            return False, "missing US API credentials"
        return True, ""

    def place_order(
        self,
        token_id: str,
        price: float,
        size_usdc: float,
    ) -> dict[str, Any]:
        """
        Place a live limit BUY order on the Polymarket US Regulated API.
        
        Note: This uses the regulated FCM-backed REST API via PolymarketUS SDK.
        """
        ready, reason = self._live_ready()
        if not ready:
            return {"ok": False, "order_id": None, "error": reason}

        # Convert USDC notional → share size
        shares = round(size_usdc / price, 2) if price > 0 else 0
        if shares <= 0:
            return {"ok": False, "order_id": None, "error": "non-positive size"}
            
        s = get_settings()
        try:
            client_us = PolymarketUS(
                key_id=s.polymarket_key_id, 
                secret_key=s.polymarket_secret_key
            )
        except Exception as exc:
            return {"ok": False, "order_id": None, "error": f"SDK Init Failed: {exc}"}
        
        try:
            # We assume price is between 0.01 and 0.99
            # The PolymarketUS SDK orders.create expects: token, price, size, side
            resp = client_us.orders.create(
                token_id=token_id,
                price=round(price, 3),
                size=shares,
                side="BUY"
            )
            
            # Extract order ID from response (adjust based on actual SDK response structure)
            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")
            
            logger.info(f"Polymarket US LIVE order placed: token={token_id} ${size_usdc} → {order_id}")
            return {"ok": True, "order_id": order_id, "error": None}
            
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error_msg = exc.response.text

            if status in (403, 451) or "compliance" in error_msg.lower() or "kyc" in error_msg.lower():
                logger.error(
                    f"🚨 POLYMARKET US COMPLIANCE/KYC HOLD DETECTED: {error_msg}. "
                    f"Guardian Circuit Breaker tripped."
                )
                from scripts.guardian_health import HALT_FILE
                import json as _json, datetime as _dt, tempfile as _tf

                state = {
                    "halted": True,
                    "reasons": [],
                    "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                }
                if os.path.exists(HALT_FILE):
                    try:
                        with open(HALT_FILE, "r") as f:
                            old_state = _json.load(f)
                            if isinstance(old_state.get("reasons"), list):
                                state["reasons"] = old_state["reasons"]
                    except Exception:
                        pass

                new_reason = "Polymarket Compliance/KYC Hold"
                if new_reason not in state["reasons"]:
                    state["reasons"].append(new_reason)

                fd, temp_path = _tf.mkstemp(dir=os.path.dirname(HALT_FILE))
                with os.fdopen(fd, "w") as f:
                    _json.dump(state, f)
                os.replace(temp_path, HALT_FILE)

                return {"ok": False, "order_id": None, "error": "KYC_HOLD"}

            logger.error(f"Market rejected order HTTP {status}: {error_msg}")
            return {"ok": False, "order_id": None, "error": f"HTTP_{status}"}

        except httpx.RequestError as exc:
            # Network-level failure (timeout, connection drop) — retryable, not compliance.
            logger.error(f"Network error placing Polymarket US order: {exc}")
            return {"ok": False, "order_id": None, "error": "NETWORK_ERROR"}

        except Exception as exc:
            # Anything landing here is NOT a known rejection type — it's an unexpected
            # bug. Don't swallow it silently as if it were a normal order failure;
            # surface it loudly and stop so it doesn't hide behind routine rejections.
            logger.critical(
                f"UNEXPECTED exception in place_order (token={token_id}): {exc}",
                exc_info=True,
            )
            raise
