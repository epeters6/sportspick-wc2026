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
# Pattern to extract date from Kalshi ticker: e.g. 26JUL06 -> 2026-07-06
_KALSHI_DATE_RE = re.compile(r'-(\d{2})([A-Z]{3})(\d{2})-')
_MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}


def _detect_venue(market_id: str) -> str:
    """Auto-detect whether a market_id is Kalshi or Polymarket based on format."""
    if not market_id:
        return "polymarket"
    # Kalshi tickers are ALL-CAPS with dashes (e.g. KXHIGHTDC-26JUL06-T84)
    if _KALSHI_TICKER_RE.match(market_id):
        return "kalshi"
    return "polymarket"


def _kalshi_ticker_date(market_id: str) -> datetime | None:
    """Extract the settlement date from a Kalshi ticker like KXHIGHTDC-26JUL06-T84.
    Returns a timezone-aware datetime (UTC midnight) or None if unparseable."""
    m = _KALSHI_DATE_RE.search(market_id)
    if not m:
        return None
    try:
        yr = 2000 + int(m.group(1))
        mo = _MONTH_MAP.get(m.group(2).upper())
        day = int(m.group(3))
        if mo is None:
            return None
        return datetime(yr, mo, day, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None



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

        # ── Stale-bet expiry ──────────────────────────────────────────────
        # For Kalshi tickers with an encoded date (e.g. KXHIGHTDC-26JUL06-T84),
        # or Polymarket slugs with a date (tc-temp-mdwhigh-2026-07-06-*),
        # expire 2 days after the market's settlement date.
        # Fallback: expire 3 days after creation for any other stale bets.

        expiry_triggered = False

        # Try Kalshi date from ticker
        kalshi_market_date = _kalshi_ticker_date(market_id) if venue == "kalshi" else None
        if kalshi_market_date is not None:
            days_since_settlement = (now - kalshi_market_date).total_seconds() / 86400
            if days_since_settlement >= 2:
                expiry_triggered = True
                reason = f"Kalshi ticker date {kalshi_market_date.date()} ({days_since_settlement:.1f}d past)"

        # Try Polymarket slug date: tc-temp-mdwhigh-2026-07-06-* or similar
        if not expiry_triggered and venue == "polymarket":
            poly_date_m = re.search(r'(\d{4}-\d{2}-\d{2})', market_id)
            if poly_date_m:
                try:
                    mkt_date = datetime.strptime(poly_date_m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    days_since_settlement = (now - mkt_date).total_seconds() / 86400
                    if days_since_settlement >= 2:
                        expiry_triggered = True
                        reason = f"Poly slug date {mkt_date.date()} ({days_since_settlement:.1f}d past)"
                except ValueError:
                    pass

        # Fallback: age from created_at
        if not expiry_triggered:
            created_raw = bet.get("created_at") or ""
            if created_raw:
                try:
                    created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                    age_days = (now - created_at).total_seconds() / 86400
                    if age_days >= 3:
                        expiry_triggered = True
                        reason = f"age {age_days:.1f}d since creation"
                except (ValueError, TypeError):
                    pass

        if expiry_triggered:
            stake = bet.get("stake") or 0.0
            try:
                db.table("autobets").update({
                    "status": "lost",
                    "pnl": round(-stake, 2),
                    "resolved_at": now.isoformat()
                }).eq("id", bet["id"]).execute()
                resolved_count += 1
                logger.info(
                    f"Expired stale weather bet {bet['id'][:8]} [{market_id}] "
                    f"-> lost ({reason})"
                )
            except Exception as e:
                logger.error(f"Failed to expire stale weather bet {bet['id']}: {e}")


    logger.info(f"Resolved {resolved_count} weather bets.")
    return resolved_count


if __name__ == "__main__":
    asyncio.run(resolve_weather_autobets())
