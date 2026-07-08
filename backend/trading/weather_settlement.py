import asyncio
import httpx
import re
from datetime import datetime, timezone, timedelta
from loguru import logger
from backend.db import get_db
from backend.trading.kalshi_client import KalshiClient

import os
import sys

# Add pavlov to path for poly_client
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../pavlov")))
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from polymarket import poly_client

# Kalshi tickers are typically uppercase: KXHIGHTDC-26JUL06-T84
_KALSHI_TICKER_RE = re.compile(r'^[A-Z]{2,}[A-Z0-9-]+$')

def _detect_venue(market_id: str) -> str:
    """Auto-detect whether a market_id is Kalshi or Polymarket based on format."""
    if not market_id:
        return "polymarket"
    # Kalshi tickers are ALL-CAPS with dashes (e.g. KXHIGHTDC-26JUL06-T84)
    if _KALSHI_TICKER_RE.match(market_id):
        return "kalshi"
    return "polymarket"


def _ensure_poly_configured():
    """Ensure poly_client can make unauthenticated requests for settlement checks."""
    if not poly_client.poly_configured():
        # Override to allow reading public settlement data without trading keys
        poly_client.poly_configured = lambda: True
        def mock_get_client():
            from polymarket_us import PolymarketUS
            return PolymarketUS(key_id="dummy", secret_key="dummy")
        poly_client.get_client = mock_get_client


async def check_polymarket_resolution(slug: str) -> dict | None:
    """Check Polymarket US API for market resolution using poly_client."""
    _ensure_poly_configured()
    try:
        # get_market_result returns 'yes' or 'no' if settled, else None
        res = poly_client.get_market_result(slug)
        if res:
            return {
                "closed": True,
                "active": False,
                "resolved": True,
                "winner": res
            }
    except Exception as e:
        logger.warning(f"Error fetching Polymarket resolution for {slug}: {e}")
    return None


async def check_kalshi_resolution(ticker: str) -> dict | None:
    """Check Kalshi API for market resolution."""
    client = KalshiClient()
    try:
        async with httpx.AsyncClient() as http_client:
            data = await client._get(http_client, f"/markets/{ticker}")
            if data and "market" in data:
                m = data["market"]
                return {
                    "closed": m.get("status") in ("closed", "settled", "determined"),
                    "resolved": m.get("status") == "settled",
                    "winner": m.get("result")  # "yes", "no", or similar
                }
    except Exception as e:
        logger.warning(f"Error fetching Kalshi resolution for {ticker}: {e}")
    return None


async def resolve_weather_autobets() -> int:
    logger.info("Starting weather autobet resolution...")
    db = get_db()

    # Fetch all open weather bets
    open_bets = (
        db.table("autobets")
        .select("*")
        .eq("bet_type", "weather")
        .eq("status", "open")
        .execute()
        .data or []
    )

    if not open_bets:
        logger.info("No open weather bets found.")
        return 0

    logger.info(f"Checking resolution for {len(open_bets)} open weather bets.")
    resolved_count = 0
    now = datetime.now(timezone.utc)

    for bet in open_bets:
        market_id = bet.get("market_id") or ""

        # Auto-detect venue from market_id format (no 'venue' column in DB yet)
        stored_venue = bet.get("venue") or ""  # might not exist → empty string
        venue = stored_venue.lower() if stored_venue else _detect_venue(market_id)

        status_data = None
        if venue == "kalshi":
            status_data = await check_kalshi_resolution(market_id)
        else:
            # Polymarket (slug-based)
            status_data = await check_polymarket_resolution(market_id)

        # If the exchange says it's settled, process it
        if status_data and (status_data.get("resolved") or status_data.get("closed")):
            winner = status_data.get("winner")
            if winner is not None:
                backed = str(bet.get("outcome_name") or "yes").lower()
                winner_str = str(winner).lower()

                won = False
                if winner_str in ("yes", "1", "true") and backed == "yes":
                    won = True
                elif winner_str in ("no", "0", "false") and backed == "no":
                    won = True

                stake = bet.get("stake") or 0.0
                shares = bet.get("shares") or 0.0
                price = bet.get("market_price") or 0.0

                new_status = "won" if won else "lost"
                new_pnl = round(shares * (1 - price), 2) if won else round(-stake, 2)

                try:
                    db.table("autobets").update({
                        "status": new_status,
                        "pnl": new_pnl,
                        "resolved_at": now.isoformat()
                    }).eq("id", bet["id"]).execute()
                    resolved_count += 1
                    logger.info(
                        f"Resolved weather bet {bet['id'][:8]} [{venue}:{market_id}] "
                        f"-> {new_status} (PnL: {new_pnl})"
                    )
                except Exception as e:
                    logger.error(f"Failed to update weather bet {bet['id']}: {e}")
            continue

        # Stale-bet expiry: if the bet is more than 5 days old and not settled,
        # assume it expired/lost so it doesn't pile up in 'open' forever.
        created_raw = bet.get("created_at") or ""
        if created_raw:
            try:
                created_at = datetime.fromisoformat(
                    created_raw.replace("Z", "+00:00")
                )
                age_days = (now - created_at).total_seconds() / 86400
                if age_days >= 5:
                    stake = bet.get("stake") or 0.0
                    try:
                        db.table("autobets").update({
                            "status": "lost",
                            "pnl": round(-stake, 2),
                            "resolved_at": now.isoformat()
                        }).eq("id", bet["id"]).execute()
                        resolved_count += 1
                        logger.info(
                            f"Expired stale weather bet {bet['id'][:8]} "
                            f"[{market_id}] (age {age_days:.1f}d) -> lost"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to expire stale weather bet {bet['id']}: {e}"
                        )
            except (ValueError, TypeError):
                pass

    logger.info(f"Resolved {resolved_count} weather bets.")
    return resolved_count


if __name__ == "__main__":
    asyncio.run(resolve_weather_autobets())
