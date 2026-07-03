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
    
    # 1. Fetch active weather markets
    try:
        # Import pavlov modules dynamically to get access to its parser
        import sys
        import os
        pavlov_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../pavlov"))
        if pavlov_path not in sys.path:
            sys.path.insert(0, pavlov_path)
            
        from polymarket import poly_client
        if not poly_client.poly_configured():
            logger.warning("POLYMARKET_KEY_ID not set. Using dummy keys for public data.")
            poly_client.poly_configured = lambda: True
            original_get_client = poly_client.get_client
            def mock_get_client():
                from polymarket_us import PolymarketUS
                return PolymarketUS(key_id="dummy", secret_key="dummy", passphrase="dummy")
            poly_client.get_client = mock_get_client
        markets = poly_client.get_weather_markets()
        logger.info(f"Fetched {len(markets)} pre-parsed Polymarket weather markets via poly_client.")
        kalshi_format_markets = markets
    except Exception as e:
        logger.error(f"Failed to fetch weather markets: {e}")
        return
        
    bankroll = 1000.0 # Arbitrary for edge calculation sizing
    
    # Run the signal engine to compute edge for ALL markets (ignoring trading thresholds and already-traded filters)
    signals = []
    for m in kalshi_format_markets:
        try:
            sig = signal_engine.calculate_edge(m, bankroll)
            if sig:
                signals.append(sig)
        except Exception as e:
            logger.warning(f"Failed to calculate edge for {m.get('ticker')}: {e}")
            
    if not signals:
        logger.info("No parseable weather markets found.")
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
