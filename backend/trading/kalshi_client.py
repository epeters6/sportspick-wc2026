"""
Kalshi data + execution client.

Read path (always available, no auth for public data):
  - Markets API (https://external-api.kalshi.com/trade-api/v2)

Write path (gated behind RSA credentials):
  - Kalshi POST endpoints.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.config import get_settings
from backend.trading.polymarket_client import PolyMarket, Outcome

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Tracks monotonic time of the last request to avoid 429s (Kalshi has strict limits)
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.5  # 500ms

class KalshiClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._private_key = self._load_private_key()

    def _load_private_key(self) -> RSAPrivateKey | None:
        raw_path = self.settings.KALSHI_PRIVATE_KEY_PATH
        if not raw_path:
            return None
            
        try:
            with open(raw_path, "rb") as fh:
                raw_bytes = fh.read()
            if raw_bytes.startswith(b"\xef\xbb\xbf"):
                raw_bytes = raw_bytes[3:]
            raw_bytes = raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            text = raw_bytes.decode("ascii", errors="ignore").strip()
            
            if not text.startswith("-----"):
                b64_clean = "".join(text.split())
                lines = [b64_clean[i:i+64] for i in range(0, len(b64_clean), 64)]
                text = "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END RSA PRIVATE KEY-----\n"
                
            pem_bytes = text.encode("utf-8")
            key = serialization.load_pem_private_key(pem_bytes, password=None)
            if isinstance(key, RSAPrivateKey):
                return key
        except Exception as exc:
            logger.warning(f"KalshiClient: failed to load RSA private key: {exc}")
        return None

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        if not self._private_key or not self.settings.KALSHI_API_KEY:
            return {}
            
        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + method.upper() + path).encode("utf-8")
        signature_bytes = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        signature_b64 = base64.b64encode(signature_bytes).decode("utf-8")
        
        return {
            "KALSHI-ACCESS-KEY": self.settings.KALSHI_API_KEY,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
        }

    async def _rate_limit(self) -> None:
        import asyncio
        global _last_request_time
        elapsed = time.monotonic() - _last_request_time
        if elapsed < _MIN_REQUEST_GAP:
            await asyncio.sleep(_MIN_REQUEST_GAP - elapsed)
        _last_request_time = time.monotonic()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _get(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
        await self._rate_limit()
        headers = self._sign_request("GET", f"/trade-api/v2{path}")
        r = await client.get(f"{BASE_URL}{path}", params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    async def fetch_markets(
        self,
        *,
        tag_slug: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> list[PolyMarket]:
        markets: list[PolyMarket] = []
        async with httpx.AsyncClient(timeout=20) as client:
            params: dict[str, Any] = {
                "status": "open",
                "limit": limit,
            }
            if search:
                params["series_ticker"] = search.upper()

            try:
                data = await self._get(client, "/markets", params)
                raw_markets = data.get("markets", [])
            except Exception as exc:
                logger.warning(f"Kalshi fetch_markets failed: {exc}")
                return []
            
            received_at = datetime.now(timezone.utc)

            for raw in raw_markets:
                pm = self._parse_market(raw, received_at)
                if pm and pm.accepting_orders and not pm.closed:
                    markets.append(pm)

        logger.info(f"Kalshi: fetched {len(markets)} tradeable markets")
        return markets

    @staticmethod
    def _parse_market(raw: dict, received_at: datetime | None = None) -> PolyMarket | None:
        try:
            # Kalshi returns yes/no markets usually.
            yes_ask = raw.get("yes_ask", 0)
            yes_bid = raw.get("yes_bid", 0)
            no_ask = raw.get("no_ask", 0)
            no_bid = raw.get("no_bid", 0)
            
            # Kalshi prices are in cents (1-99), convert to (0-1) range.
            yes_price = yes_ask / 100.0 if yes_ask else (yes_bid / 100.0 if yes_bid else 0.5)
            no_price = no_ask / 100.0 if no_ask else (no_bid / 100.0 if no_bid else 0.5)

            outcomes = [
                Outcome(name="Yes", token_id="yes", price=yes_price, best_bid=yes_bid/100.0 if yes_bid else None, best_ask=yes_ask/100.0 if yes_ask else None),
                Outcome(name="No", token_id="no", price=no_price, best_bid=no_bid/100.0 if no_bid else None, best_ask=no_ask/100.0 if no_ask else None),
            ]

            return PolyMarket(
                market_id=str(raw.get("ticker", "")),
                gamma_id=str(raw.get("ticker", "")),
                slug=str(raw.get("ticker", "")),
                question=str(raw.get("title", "")),
                outcomes=outcomes,
                liquidity=float(raw.get("liquidity") or 0) / 100.0,
                volume_24h=float(raw.get("volume_24h") or 0) / 100.0,
                end_date=raw.get("close_time"),
                accepting_orders=True, # status=open implies accepting orders
                closed=False,
                received_timestamp=received_at or datetime.now(timezone.utc),
                exchange_timestamp=None, # Kalshi GET /markets doesn't have a reliable last updated at
            )
        except Exception as exc:
            logger.debug(f"Could not parse Kalshi market: {exc}")
            return None

    async def get_book_depth(self, token_id: str, market_id: str, side: str = "sell") -> tuple[float | None, float]:
        """
        Fetch Kalshi order book depth.
        Kalshi order books are fetched per market. token_id here is 'yes' or 'no'.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                book = await self._get(client, f"/markets/{market_id}/orderbook")
        except Exception as exc:
            logger.debug(f"Kalshi book fetch failed for {market_id}: {exc}")
            return None, 0.0

        kalshi_side = "yes" if token_id.lower() == "yes" else "no"
        book_key = f"{kalshi_side}_asks" if side == "sell" else f"{kalshi_side}_bids"
        levels = book.get("orderbook", {}).get(book_key, [])
        
        if not levels:
            return None, 0.0

        # Kalshi returns levels as [price_cents, quantity] pairs.
        # Aggregate top 3 levels like Polymarket, in USDC notional
        # (price in dollars x contracts) so risk liquidity gates compare
        # like-for-like with Polymarket book depth.
        total_size_usdc = 0.0
        best_price = None
        for i, level in enumerate(levels[:3]):
            price, qty = level[0], level[1]
            if i == 0:
                best_price = price / 100.0
            total_size_usdc += (price / 100.0) * qty

        return best_price, total_size_usdc

