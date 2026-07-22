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

# Official Kalshi MLB game series — never substitute a free-text team name.
KALSHI_MLB_SERIES_TICKER = "KXMLBGAME"

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

        # SECURITY_NOTE: prefer a secrets manager / CI secret file outside the
        # git tree. A repo-relative PEM path is a leak risk (archives, backups).
        try:
            abs_path = os.path.abspath(raw_path)
            repo_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            if abs_path == repo_root or abs_path.startswith(repo_root + os.sep):
                logger.warning(
                    "KalshiClient: private key path is under the repository "
                    f"({os.path.basename(abs_path)}). Prefer a secrets manager "
                    "or an absolute path outside the repo; rotate the key if it "
                    "was ever packaged in a zip or commit."
                )
        except Exception:
            pass

        try:
            with open(raw_path, "rb") as fh:
                raw_bytes = fh.read()
            if raw_bytes.startswith(b"\xef\xbb\xbf"):
                raw_bytes = raw_bytes[3:]
            raw_bytes = raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            text = raw_bytes.decode("ascii", errors="ignore").strip()

            if not text.startswith("-----"):
                b64_clean = "".join(text.split())
                lines = [b64_clean[i : i + 64] for i in range(0, len(b64_clean), 64)]
                text = (
                    "-----BEGIN RSA PRIVATE KEY-----\n"
                    + "\n".join(lines)
                    + "\n-----END RSA PRIVATE KEY-----\n"
                )

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
        series_ticker: str | None = None,
        limit: int = 200,
    ) -> list[PolyMarket]:
        """
        Fetch open Kalshi markets.

        ``series_ticker`` must be a real Kalshi series code (e.g. KXMLBGAME).
        Free-text ``search`` is never sent as series_ticker.
        """
        markets: list[PolyMarket] = []
        async with httpx.AsyncClient(timeout=20) as client:
            params: dict[str, Any] = {
                "status": "open",
                "limit": limit,
            }
            if series_ticker:
                params["series_ticker"] = str(series_ticker).upper()
            elif search:
                # Do not pass team names / free text as series_ticker.
                logger.warning(
                    "Kalshi fetch_markets: refusing free-text search=%r as series_ticker; "
                    "use series_ticker= or fetch_mlb_game_markets()",
                    search,
                )
                return []

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

    async def fetch_mlb_game_markets(self, *, limit: int = 200) -> list[PolyMarket]:
        """
        Discover current MLB game-winner markets via the events/series API,
        then parse nested markets. Filter locally by teams/date in the matcher.
        """
        markets: list[PolyMarket] = []
        async with httpx.AsyncClient(timeout=30) as client:
            cursor: str | None = None
            fetched_events = 0
            while fetched_events < limit:
                params: dict[str, Any] = {
                    "status": "open",
                    "series_ticker": KALSHI_MLB_SERIES_TICKER,
                    "with_nested_markets": "true",
                    "limit": min(200, limit - fetched_events),
                }
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = await self._get(client, "/events", params)
                except Exception as exc:
                    logger.warning(f"Kalshi MLB events fetch failed: {exc}")
                    break
                received_at = datetime.now(timezone.utc)
                events = data.get("events") or []
                if not events:
                    break
                for evt in events:
                    fetched_events += 1
                    evt_title = str(evt.get("title") or "")
                    evt_ticker = str(evt.get("event_ticker") or evt.get("ticker") or "")
                    nested = evt.get("markets") or []
                    # Some responses put markets at top-level keyed by event
                    for raw in nested:
                        raw = dict(raw)
                        if not raw.get("title") and evt_title:
                            raw["title"] = evt_title
                        if not raw.get("event_ticker"):
                            raw["event_ticker"] = evt_ticker
                        pm = self._parse_market(raw, received_at, event_title=evt_title)
                        if pm and pm.accepting_orders and not pm.closed:
                            pm.venue = "kalshi"
                            markets.append(pm)
                cursor = data.get("cursor") or None
                if not cursor:
                    break

        logger.info(f"Kalshi: fetched {len(markets)} MLB game markets via {KALSHI_MLB_SERIES_TICKER}")
        return markets

    @staticmethod
    def _parse_market(
        raw: dict,
        received_at: datetime | None = None,
        event_title: str | None = None,
    ) -> PolyMarket | None:
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

            ticker = str(raw.get("ticker", ""))
            title = str(raw.get("title") or event_title or "")
            yes_team = None
            # Ticker pattern ...-TEAM (YES side)
            import re
            from backend.models.sports.mlb_moneyline_match import _KALSHI_ABBR_TO_TEAM

            m = re.search(r"-([A-Z]{2,3})$", ticker.upper())
            if m:
                yes_team = _KALSHI_ABBR_TO_TEAM.get(m.group(1))

            return PolyMarket(
                market_id=ticker,
                gamma_id=ticker,
                slug=ticker,
                question=title,
                outcomes=outcomes,
                liquidity=float(raw.get("liquidity") or 0) / 100.0,
                volume_24h=float(raw.get("volume_24h") or 0) / 100.0,
                end_date=raw.get("close_time") or raw.get("expected_expiration_time"),
                accepting_orders=True,  # status=open implies accepting orders
                closed=False,
                venue="kalshi",
                received_timestamp=received_at or datetime.now(timezone.utc),
                exchange_timestamp=None,
                yes_proposition_team=yes_team,
            )
        except Exception as exc:
            logger.debug(f"Could not parse Kalshi market: {exc}")
            return None

    async def get_top_of_book(self, token_id: str, market_id: str) -> dict:
        """Real Kalshi top-of-book for yes/no token. No fabricated bids/spreads."""
        empty = {
            "best_bid": None,
            "best_ask": None,
            "bid_size": 0.0,
            "ask_size": 0.0,
            "book_timestamp": None,
            "received_timestamp": None,
            "missing_orderbook_timestamp": True,
            "timestamp_source": None,
        }
        received_at: datetime | None = None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await self._rate_limit()
                headers = self._sign_request("GET", f"/trade-api/v2/markets/{market_id}/orderbook")
                r = await client.get(
                    f"{BASE_URL}/markets/{market_id}/orderbook", headers=headers
                )
                r.raise_for_status()
                # Capture receipt at the real HTTP response boundary
                received_at = datetime.now(timezone.utc)
                book = r.json()
        except Exception as exc:
            logger.debug(f"Kalshi book fetch failed for {market_id}: {exc}")
            return empty

        kalshi_side = "yes" if str(token_id).lower() in {"yes", "y"} else "no"
        ob = book.get("orderbook", {}) if isinstance(book, dict) else {}
        asks = ob.get(f"{kalshi_side}_asks") or []
        bids = ob.get(f"{kalshi_side}_bids") or []
        try:
            best_ask = float(asks[0][0]) / 100.0 if asks else None
            ask_size = float(asks[0][1]) if asks else 0.0
            best_bid = float(bids[0][0]) / 100.0 if bids else None
            bid_size = float(bids[0][1]) if bids else 0.0
        except (TypeError, ValueError, IndexError):
            return {**empty, "received_timestamp": received_at}
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            # Kalshi orderbook payload has no exchange timestamp — do not invent one.
            "book_timestamp": None,
            "received_timestamp": received_at,
            "missing_orderbook_timestamp": True,
            "timestamp_source": "received_timestamp",
        }

    async def get_book_depth(self, token_id: str, market_id: str, side: str = "sell") -> tuple[float | None, float]:
        """
        Fetch Kalshi order book depth.
        Kalshi order books are fetched per market. token_id here is 'yes' or 'no'.
        Depth is top-of-book contract size (shares), not fabricated notional.
        """
        book = await self.get_top_of_book(token_id=token_id, market_id=market_id)
        if side == "sell":
            return book.get("best_ask"), float(book.get("ask_size") or 0.0)
        return book.get("best_bid"), float(book.get("bid_size") or 0.0)

