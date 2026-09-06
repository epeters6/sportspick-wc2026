import asyncio
import math
import os
import sys
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pavlov.pipeline.clv_updater import (
    update_clv_checkpoints,
    update_clv_obligations,
    count_clv_obligations,
)

def _binary_weather_price(book: dict, side: str):
    """Buy YES at its ask; buy NO at one minus the executable YES bid."""
    field, size_field = ("best_ask", "yes_ask_size") if side == "YES" else ("best_bid", "yes_bid_size")
    try:
        price = float(book.get(field))
        size = float(book.get(size_field, book.get("ask_size" if side == "YES" else "bid_size", 0)))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or not math.isfinite(size) or size <= 0 or not 0 < price < 1:
        return None
    return price if side == "YES" else round(1.0 - price, 10)


async def _fetch_executable_price(
    market_id: str,
    outcome_id: str,
    side: str,
    router=None,
):
    """
    Side-correct executable price for the purchased outcome token.

    Weather contracts use public venue books. Legacy token IDs retain their
    original router. A US slug must never be sent to the international CLOB.
    Returns (price, book_timestamp, received_timestamp).
    """
    venue = "kalshi" if str(market_id).upper().startswith("KX") else "polymarket"
    try:
        binary_outcome = str(outcome_id).lower() in {"yes", "no"}
        weather_kalshi = str(market_id).upper().startswith(("KXHIGH", "KXLOW"))
        if weather_kalshi or (venue == "polymarket" and binary_outcome):
            side = str(side).upper()
            if side not in {"YES", "NO"}:
                return None, None, None
            if weather_kalshi:
                from pavlov.pipeline import kalshi_client

                book = await asyncio.to_thread(kalshi_client.get_orderbook_as_parsed, market_id)
            else:
                from pavlov.polymarket import poly_client

                book = await asyncio.to_thread(poly_client.get_orderbook_as_parsed, market_id)
            if not isinstance(book, dict) or book.get("ticker") != market_id:
                return None, None, None
            return (
                _binary_weather_price(book, side),
                book.get("orderbook_timestamp"),
                book.get("received_timestamp"),
            )
        if router is None:
            from backend.trading.venue_router import VenueRouter

            router = VenueRouter()
        book = await router.get_top_of_book(
            venue=venue,
            token_id=outcome_id,
            market_id=market_id,
        )
        price = book.get("best_ask")
        book_ts = book.get("book_timestamp")
        received_ts = book.get("received_timestamp")
        if price is None:
            return None, book_ts, received_ts
        return float(price), book_ts, received_ts
    except Exception as e:
        logger.warning(f"Failed to fetch price for CLV {market_id} {outcome_id}: {e}")
        return None, None, None


async def run_scheduler(once: bool = False):
    logger.info("Starting CLV Checkpoint Scheduler...")
    # New weather observations need no trading client or signing credentials.
    router = None

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
