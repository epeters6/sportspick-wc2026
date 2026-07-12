import asyncio
import httpx
import tempfile
import os
from loguru import logger
from backend.db import get_db
from backend.trading.clv_tracker import calculate_clv

async def backfill_clv():
    db = get_db()
    
    # Fetch all resolved bets missing a closing price OR that were previously backfilled with bad data
    response = db.table("autobets").select("*").in_("status", ["won", "lost"]).is_("closing_price", "null").execute()
    bets = response.data or []
    
    # Also grab rows that were previously backfilled, to overwrite the poisoned hourly data
    response_bad = db.table("autobets").select("*").eq("clv_source", "backfilled").execute()
    existing_ids = {b["id"] for b in bets}
    bets.extend([b for b in (response_bad.data or []) if b["id"] not in existing_ids])
    
    logger.info(f"Found {len(bets)} resolved bets to backfill CLV.")
    
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
                # Fetch true 1-minute price history from Polymarket CLOB.
                # fidelity=1 (1 minute buckets), interval=max (all time)
                lookup_id = token_id or market_id
                url = (
                    f"https://clob.polymarket.com/prices-history"
                    f"?market={lookup_id}&interval=max&fidelity=1"
                )
                resp = await client.get(url)
                
                if resp.status_code != 200:
                    logger.warning(f"Failed to fetch history for {market_id}: HTTP {resp.status_code}")
                    skipped_api_fail += 1
                    continue
                    
                data = resp.json()
                history = data.get("history", [])
                if not history:
                    logger.warning(f"No price history returned for {market_id} (API empty response). Skipping.")
                    skipped_no_history += 1
                    continue
                
                actual_closing_price = float(history[-1].get("p", 0.50))
                
                clv = calculate_clv(float(market_price), actual_closing_price)
                
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
