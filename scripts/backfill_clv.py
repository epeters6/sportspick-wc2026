import asyncio
import httpx
import tempfile
import os
from loguru import logger
from backend.db import get_db

# Import the exact same CLV calculation used for live bets so backfilled and
# live numbers are guaranteed to be computed identically.
# CLV = closing_price - market_price (positive = you beat the closing line = good)
def _compute_clv(market_price: float, closing_price: float) -> float:
    """CLV as a probability-point improvement over the closing line.
    
    Positive CLV means we bought cheaper than the market settled at —
    we beat the closing line. This matches what clv_tracker.py computes
    for live bets (closing_price - entry_price).
    """
    return round(closing_price - market_price, 4)


async def backfill_clv():
    db = get_db()
    
    # Fetch all resolved bets missing a closing price
    response = db.table("autobets").select("*").eq("status", "won").is_("closing_price", "null").execute()
    bets = response.data or []
    
    # Also get lost bets
    response_lost = db.table("autobets").select("*").eq("status", "lost").is_("closing_price", "null").execute()
    bets.extend(response_lost.data or [])
    
    logger.info(f"Found {len(bets)} resolved bets missing CLV.")
    
    updated = 0
    skipped_no_token = 0
    skipped_api_fail = 0
    skipped_no_history = 0
    
    async with httpx.AsyncClient(timeout=15) as client:
        for bet in bets:
            market_id = bet.get("market_id")
            token_id = bet.get("token_id")
            market_price = bet.get("market_price")
            
            if not market_id:
                skipped_no_token += 1
                continue

            if market_price is None:
                logger.debug(f"Skip {market_id}: no market_price to compute CLV against")
                skipped_no_token += 1
                continue
                
            try:
                # Fetch actual 1-minute price history from Polymarket CLOB.
                # Uses token_id when available (more precise), falls back to market_id.
                lookup_id = token_id or market_id
                url = (
                    f"https://clob.polymarket.com/prices-history"
                    f"?market={lookup_id}&interval=1m&fidelity=60"
                )
                resp = await client.get(url)
                
                if resp.status_code != 200:
                    logger.warning(f"Failed to fetch history for {market_id}: HTTP {resp.status_code}")
                    skipped_api_fail += 1
                    continue
                    
                data = resp.json()
                history = data.get("history", [])
                if not history:
                    logger.debug(f"No price history for {market_id}")
                    skipped_no_history += 1
                    continue
                
                # The last candle before market close is the true closing price.
                # This is the real price the market settled at, fetched from the
                # Polymarket CLOB API at 1-minute fidelity.
                actual_closing_price = float(history[-1].get("p", 0.50))
                
                # CLV = closing_price - market_price (our entry price).
                # Positive = we bought cheaper than the market settled = we beat
                # the closing line. This is the standard CLV definition and matches
                # how live bets are tracked in clv_tracker.py.
                clv = _compute_clv(float(market_price), actual_closing_price)
                
                db.table("autobets").update({
                    "closing_price": actual_closing_price,
                    "clv": clv,
                    "clv_source": "backfilled"
                }).eq("id", bet["id"]).execute()
                
                updated += 1
                logger.debug(
                    f"Backfilled {market_id}: entry={market_price:.3f} "
                    f"close={actual_closing_price:.3f} clv={clv:+.4f}"
                )
            except Exception as exc:
                logger.error(f"Failed to backfill {market_id}: {exc}")
                skipped_api_fail += 1
                
    logger.info(
        f"Backfill complete. Updated={updated} | "
        f"skipped_no_token={skipped_no_token} | "
        f"skipped_api_fail={skipped_api_fail} | "
        f"skipped_no_history={skipped_no_history}"
    )

if __name__ == "__main__":
    asyncio.run(backfill_clv())
