"""
Sync weather model predictions to the unified model_predictions table.
Uses the ported Pavlov signal engine to evaluate weather edge.
"""
from loguru import logger
import traceback

from backend.db import get_db
from backend.trading.polymarket_client import PolymarketClient

import sys
import os
# Add the pavlov directory to the path so we can import the pipeline modules directly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../pavlov")))

# Bypass Pavlov's required env vars since we aren't trading via Kalshi here
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"

from pipeline import signal_engine, owm_client

async def sync_weather_predictions():
    logger.info("Starting weather prediction sync...")
    db = get_db()
    client = PolymarketClient()
    
    # 1. Fetch active weather markets from Polymarket
    # Polymarket uses tags or search terms for weather.
    markets = []
    try:
        markets = await client.fetch_markets(search="temperature")
        markets.extend(await client.fetch_markets(search="weather"))
    except Exception as e:
        logger.error(f"Failed to fetch weather markets: {e}")
        return

    # Deduplicate markets by ID
    unique_markets = {m.market_id: m for m in markets}.values()
    
    logger.info(f"Found {len(unique_markets)} weather-related markets on Polymarket.")
    
    # We will need to map Polymarket markets into the format the signal engine expects
    # The signal engine parse_market expects a dict with 'title', 'close_time', 'yes_ask', 'yes_bid', etc.
    kalshi_format_markets = []
    for m in unique_markets:
        # Polymarket markets can have multiple outcomes, we only want binary YES/NO for temp
        if len(m.outcomes) != 2:
            continue
            
        yes_outcome = next((o for o in m.outcomes if o.name.lower() == "yes"), None)
        no_outcome = next((o for o in m.outcomes if o.name.lower() == "no"), None)
        
        if not yes_outcome or not no_outcome:
            continue

        yes_ask = (yes_outcome.best_ask or yes_outcome.mid_price or 0.5) * 100
        yes_bid = (yes_outcome.best_bid or yes_outcome.mid_price or 0.5) * 100

        k_mkt = {
            "ticker": m.market_id,
            "title": m.question,
            "close_time": m.end_date_iso, # Need to map to correct field
            "yes_ask": yes_ask,
            "yes_bid": yes_bid,
            "open_interest": m.liquidity,
            "venue": "poly_us"
        }
        kalshi_format_markets.append(k_mkt)
        
    bankroll = 1000.0 # Arbitrary for edge calculation sizing
    
    # Run the signal engine to compute edge
    signals = signal_engine.get_all_signals(kalshi_format_markets, bankroll)
    
    if not signals:
        logger.info("No actionable weather signals found.")
        return
        
    logger.info(f"Generated {len(signals)} weather predictions. Writing to model_predictions...")
    
    inserted = 0
    for sig in signals:
        # Construct the event key (e.g. NYC-high-2026-07-03)
        event_key = f"{sig['city']}-{sig['metric']}-{sig['market_date']}"
        outcome = sig['direction'] # "above", "below", "in_range"
        
        metadata = {
            "market_id": sig['ticker'],
            "nws_predicted": sig['nws_predicted'],
            "owm_predicted": sig['owm_predicted'],
            "ensemble_members": sig['ensemble_members'],
            "threshold_f": sig['threshold_f']
        }
        
        try:
            # Delete old prediction if it exists
            db.table("model_predictions").delete().eq("source", "weather_model").eq("event_key", event_key).eq("outcome", outcome).execute()
            
            # Insert new prediction
            db.table("model_predictions").insert({
                "source": "weather_model",
                "domain": "weather",
                "event_key": event_key,
                "outcome": outcome,
                "prob": sig['model_prob'],
                "market_price": sig['implied_prob'],
                "edge": sig['edge'],
                "metadata": metadata
            }).execute()
            inserted += 1
        except Exception as e:
            logger.error(f"Failed to insert weather prediction {event_key}: {e}")
            
    logger.info(f"Successfully synced {inserted} weather predictions.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(sync_weather_predictions())
