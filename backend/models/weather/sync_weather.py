"""
Sync weather model predictions to the unified model_predictions table.
Uses the ported Pavlov signal engine to evaluate weather edge.
"""
from loguru import logger
import traceback

from backend.db import get_db
from backend.trading.polymarket_client import PolymarketClient
from backend.trading.autobet import _current_bankroll

import sys
import os
# Add the pavlov directory to the path so we can import the pipeline modules directly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../pavlov")))

# Bypass Pavlov's required env vars since we aren't trading via Kalshi here
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from backend.config import get_settings
s = get_settings()
os.environ["KELLY_FRACTION"] = str(s.polymarket_kelly_multiplier)

from pipeline import signal_engine, owm_client

async def sync_weather_predictions():
    logger.info("Starting weather prediction sync & autobet integration...")
    db = get_db()
    
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
                return PolymarketUS(key_id="dummy", secret_key="dummy")
            poly_client.get_client = mock_get_client
        markets = poly_client.get_weather_markets()
        logger.info(f"Fetched {len(markets)} pre-parsed Polymarket weather markets via poly_client.")
        
        try:
            from pipeline import kalshi_client
            kalshi_markets = kalshi_client.get_weather_markets()
            logger.info(f"Fetched {len(kalshi_markets)} pre-parsed Kalshi weather markets via kalshi_client.")
            markets.extend(kalshi_markets)
        except Exception as e:
            logger.warning(f"Failed to fetch Kalshi weather markets: {e}")
            
        combined_markets = markets
    except Exception as e:
        logger.error(f"Failed to fetch weather markets: {e}")
        return
        
    # Get true shared bankroll
    bankroll = _current_bankroll(db)
    
    # Run the signal engine to compute edge for ALL markets
    signals = []
    for m in combined_markets:
        try:
            sig = signal_engine.calculate_edge(m, bankroll, trading_mode=False)
            if sig:
                signals.append(sig)
        except Exception as e:
            logger.warning(f"Failed to calculate edge for {m.get('ticker')}: {e}")
            
    if not signals:
        logger.info("No parseable weather markets found.")
        return
        
    logger.info(f"Generated {len(signals)} weather predictions.")
    
    inserted = 0
    bets_placed = 0
    from backend.config import get_settings
    s = get_settings()
    mode = "live" if s.polymarket_live_enabled else "paper"
    
    for sig in signals:
        # Construct the event key (e.g. NYC-high-2026-07-03)
        event_key = f"{sig['city']}-{sig['metric']}-{sig['market_date']}"
        outcome = sig['direction'] # "above", "below", "in_range"
        
        metadata = {
            "market_id": sig['ticker'],
            "nws_predicted": sig.get('nws_predicted'),
            "owm_predicted": sig.get('owm_predicted'),
            "ensemble_members": sig.get('ensemble_members'),
            "threshold_f": sig.get('threshold_f'),
            "z_score": sig.get('z_score', 0.0),
            "raw_model_prob": sig.get('raw_model_prob', sig['model_prob']),
            "suppressed_reason": sig.get("suppressed_reason")
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

        # --- Autobet Integration ---
        
    logger.info("Deduplicating signals by city, metric, and date to prevent Kalshi strike spam...")
    best_signals = {}
    for sig in signals:
        if sig.get("suppressed_reason") or sig.get("edge", 0) <= 0 or sig.get("kelly_dollars", 0) <= 0:
            continue
        key = (sig["city"], sig["metric"], sig["market_date"])
        if key not in best_signals or sig["kelly_dollars"] > best_signals[key]["kelly_dollars"]:
            best_signals[key] = sig
            
    valid_signals = list(best_signals.values())
    logger.info(f"Filtered down to {len(valid_signals)} top signals after deduplicating strikes and removing suppressed/0-edge.")
    
    # Pre-fetch existing exposure for open weather bets
    exposure_tracker = {}
    open_bets = db.table("autobets").select("bet_subject, stake").eq("status", "open").like("bet_subject", "weather_%").execute()
    for row in (open_bets.data or []):
        subj = row.get("bet_subject")
        exposure_tracker[subj] = exposure_tracker.get(subj, 0.0) + (row.get("stake") or 0.0)
        
    # Determine max exposure per event
    paper_max_dollars = bankroll * s.polymarket_paper_max_position_pct
    live_max_dollars = bankroll * s.polymarket_max_position_pct

    for sig in valid_signals:
        stake = sig["kelly_dollars"]
        market_id = sig["ticker"]
        outcome_name = "yes"
        # Find original market to get token_id
        m = next((mx for mx in combined_markets if mx.get("ticker") == market_id or str(mx.get("condition_id")) == market_id), {})

        token_id = m.get("yes_token", "unknown")
        # Fallback for Polymarket format if it's a dict
        if token_id == "unknown" and "outcomes" in m:
            for out in m.get("outcomes", []):
                if isinstance(out, dict) and out.get("name", "").lower() == "yes":
                    token_id = out.get("token_id", "unknown")
                    break
            
        shares = round(stake / sig["implied_prob"], 2) if sig["implied_prob"] > 0 else 0
        sport_tier = "weather_far_tail" if sig.get("z_score", 0.0) >= 2.0 else ("weather_near_tail" if sig.get("z_score", 0.0) >= 1.0 else "weather_mode")
        
        # Hard go-live gating: these tiers are unproven under the new CLV system and need fresh paper trading
        bet_mode = mode
        if sport_tier in ("weather_near_tail", "weather_far_tail"):
            bet_mode = "paper"
            
        virtual_match_id = f"{sport_tier}_{sig.get('market_date', '')}"
        
        # Enforce Event Exposure Cap
        max_allowed = live_max_dollars if bet_mode == "live" else paper_max_dollars
        current_exposure = exposure_tracker.get(virtual_match_id, 0.0)
        
        if current_exposure >= max_allowed:
            logger.info(f"Skipping {market_id}: {virtual_match_id} exposure (${current_exposure:.2f}) is at/over cap (${max_allowed:.2f})")
            continue
            
        if current_exposure + stake > max_allowed:
            stake = max_allowed - current_exposure
            logger.info(f"Scaling down {market_id} stake to ${stake:.2f} to fit {virtual_match_id} exposure cap")
            
        # Update running exposure
        exposure_tracker[virtual_match_id] += stake
        shares = round(stake / sig["implied_prob"], 2) if sig["implied_prob"] > 0 else 0
            
        record = {
            "bet_subject": virtual_match_id,
            "market_id": market_id,
            "market_slug": market_id,
            "question": f"Weather: {sig['city']} {sig['metric']} {outcome} {sig.get('threshold_f')}",
            "outcome_name": outcome_name,
            "token_id": token_id,
            "mode": bet_mode,
            "model_prob": sig["model_prob"],
            "market_prob": sig["implied_prob"],
            "market_price": sig["implied_prob"], # We don't fetch order book depth here currently, assume implied
            "edge": sig["edge"],
            "raw_confidence": sig.get("raw_model_prob", sig["model_prob"]),
            "sport": sport_tier,
            "kelly_fraction": sig.get("kelly_fraction", 0),
            "stake": stake,
            "bankroll_at_time": round(bankroll, 2),
            "shares": shares,
            "status": "open",
            "bet_type": "weather",
        }
        
        # Check if bet is already open
        existing = db.table("autobets").select("id").eq("market_id", market_id).eq("outcome_name", outcome_name).eq("mode", bet_mode).eq("status", "open").execute()
        if not existing.data:
            try:
                db.table("autobets").insert(record).execute()
                bets_placed += 1
            except Exception as e:
                logger.error(f"Failed to record weather autobet: {e}")

    logger.info(f"Successfully synced {inserted} predictions, recorded {bets_placed} new {mode} bets.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(sync_weather_predictions())
