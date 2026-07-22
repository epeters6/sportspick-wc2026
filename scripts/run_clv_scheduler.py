import asyncio
import os
import sys
from datetime import datetime, timezone
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pavlov.pipeline.clv_updater import (
    update_clv_checkpoints,
    update_clv_obligations,
    count_clv_obligations,
)
from backend.trading.venue_router import VenueRouter


async def _fetch_executable_price(
    market_id: str,
    outcome_id: str,
    side: str,
    router: VenueRouter,
):
    """
    Side-correct executable price for the purchased outcome token.

    Buying YES/token → take the ask (side='sell' on the book).
    Returns (price, book_timestamp) or (None, None).
    """
    venue = "kalshi" if str(market_id).upper().startswith("KX") else "polymarket"
    try:
        book = await router.get_top_of_book(
            venue=venue,
            token_id=outcome_id,
            market_id=market_id,
        )
        # Executable for a buyer of this outcome is the ask
        price = book.get("best_ask")
        book_ts = book.get("book_timestamp")
        if price is None:
            return None, None
        return float(price), book_ts
    except Exception as e:
        logger.warning(f"Failed to fetch price for CLV {market_id} {outcome_id}: {e}")
        return None, None


async def run_scheduler(once: bool = False):
    logger.info("Starting CLV Checkpoint Scheduler...")
    router = VenueRouter()

    while True:
        try:
            logger.info("Running update_clv_obligations (Supabase)...")
            before = None
            try:
                before = count_clv_obligations()
                logger.info(f"CLV obligations before: {before}")
            except Exception as exc:
                logger.error(f"CLV obligation count failed (required): {exc}")
                raise

            stats = await update_clv_obligations(
                fetch_price=lambda mid, oid, s: _fetch_executable_price(mid, oid, s, router),
            )
            logger.info(f"CLV obligations update stats: {stats}")

            after = count_clv_obligations()
            logger.info(f"CLV obligations after: {after}")

            # Legacy jsonl artifacts (best-effort; durable path is Supabase)
            try:
                await update_clv_checkpoints(
                    fetch_price=lambda mid, oid, s: _fetch_executable_price(mid, oid, s, router),
                    filepath="sports_clv_tracking.jsonl",
                )
                if os.path.exists("clv_tracking.jsonl"):
                    await update_clv_checkpoints(
                        fetch_price=lambda mid, oid, s: _fetch_executable_price(mid, oid, s, router),
                        filepath="clv_tracking.jsonl",
                    )
            except Exception as exc:
                logger.warning(f"Legacy CLV jsonl update warning: {exc}")

        except Exception as e:
            logger.error(f"Error in CLV Scheduler: {e}")
            if once:
                raise
            # Continuous mode: log and retry next cycle

        if once:
            logger.info("Running once, exiting.")
            break

        logger.info("Sleeping for 60 seconds...")
        await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        run_once = "--once" in sys.argv
        asyncio.run(run_scheduler(once=run_once))
    except KeyboardInterrupt:
        logger.info("CLV Scheduler stopped manually.")
