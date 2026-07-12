import json
import os
import sys
from datetime import datetime, timezone
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from backend.db import get_db
from backend.trading.venue_router import VenueRouter
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.risk_caps import RiskCaps
from backend.models.sports.sync_sports import sync_sports_market
from backend.trading.autobet import _current_bankroll

async def run_mlb_shadow_execution():
    logger.info("Running MLB shadow execution...")
    manifest_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "ml", "mlb_quant", "manifest.json")
    if not os.path.exists(manifest_path):
        logger.warning(f"Manifest not found at {manifest_path}")
        return
        
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
        
    router = VenueRouter()
    db = get_db()
    bankroll = _current_bankroll(db)
    
    risk_caps = RiskCaps(
        max_event_exposure_pct=0.05,
        max_outcome_exposure_pct=0.02,
        max_strategy_exposure_pct=0.1,
        max_platform_exposure_pct=0.2,
        max_daily_loss_pct=0.05,
        max_weekly_loss_pct=0.1,
        min_net_edge=0.015,
        min_log_growth_delta=0.001
    )

    for p_key, data in manifest.items():
        if data.get("prediction") is None and data.get("prop_line") is None:
            continue
            
        team = data.get("team")
        opp = data.get("opponent")
        date_str = data.get("slate_date")
        
        # In a full system, we'd find the exact market from the router.
        # For this shadow validation, let's search for the game on Polymarket.
        term = f"{team}"
        markets = await router.fetch_markets(search=term, limit=20)
        
        target_markets = []
        for m in markets:
            if team.lower() in m.question.lower() and opp.lower() in m.question.lower():
                target_markets.append(m)
                
        if len(target_markets) > 1:
            logger.warning(f"AMBIGUOUS_TEAM_MARKET_MATCH: Multiple markets found for {team} vs {opp}")
            continue
        elif not target_markets:
            logger.info(f"Could not find Polymarket market for {team} vs {opp}")
            continue
            
        target_market = target_markets[0]
        
        if "winner" not in target_market.question.lower() and "moneyline" not in target_market.question.lower():
            logger.warning(f"UNSUPPORTED_MARKET_TYPE: Found market {target_market.question}")
            continue
            
        start_dt = datetime.now(timezone.utc)
        if getattr(target_market, "end_date", None):
            if isinstance(target_market.end_date, datetime) and target_market.end_date < datetime.now(timezone.utc):
                logger.warning("INVALID_EVENT_TIME: Stale event")
                continue
            elif isinstance(target_market.end_date, str):
                try:
                    dt = datetime.fromisoformat(target_market.end_date.replace("Z", "+00:00"))
                    if dt < datetime.now(timezone.utc):
                        logger.warning("INVALID_EVENT_TIME: Stale event")
                        continue
                except:
                    pass

        # Find YES outcome
        outcome = None
        for o in target_market.outcomes:
            if team.lower() in o.name.lower() or "yes" in o.name.lower():
                outcome = o
                break
                
        if not outcome:
            continue
            
        # Build features
        features = SportsEventFeatures(
            sport="mlb",
            league="mlb",
            event_id=f"mlb_{date_str}_{team}_{opp}".replace(" ", "_"),
            market_id=target_market.market_id,
            team_a=team,
            team_b=opp,
            start_time=datetime.now(timezone.utc), # mock for now
            snapshot_time=datetime.now(timezone.utc),
            market_prob_baseline=outcome.price,
            market_price_source="polymarket",
            elo_team_a=1500, # mock
            elo_team_b=1500,
            elo_diff=0,
            consensus_pick_count_a=0,
            consensus_pick_count_b=0,
            consensus_weighted_signal=0.0,
            source_clv_weighted_signal=0.0,
            source_count=0,
            independent_source_count=0,
            sport_specific={
                "pitcher": data.get("name"),
                "tier": data.get("tier"),
                "prop_line": data.get("prop_line"),
                "manager_hook_score": data.get("advanced_context", {}).get("manager_hook_score", 0),
            }
        )
        
        best_ask = outcome.best_ask or outcome.price
        visible_depth = target_market.liquidity
        
        real_received = getattr(target_market, "received_timestamp", datetime.now(timezone.utc))
        real_exch = getattr(target_market, "exchange_timestamp", datetime.now(timezone.utc))
        
        sync_sports_market(
            market_data={"platform": target_market.venue},
            features=features,
            best_ask=best_ask,
            fee_per_share=0.01,
            visible_depth=visible_depth,
            bankroll=bankroll,
            risk_caps=risk_caps,
            mode="shadow",
            real_orderbook_timestamp=real_exch,
            real_received_timestamp=real_received
        )
        
    logger.info("MLB shadow execution complete.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(run_mlb_shadow_execution())
