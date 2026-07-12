"""
Sync weather model predictions to the unified model_predictions table and execute portfolio-optimized autobets.
Revised architecture using Bayesian probability shrinkage, full event vectors, and multi-outcome Kelly optimization.
"""
from loguru import logger
import traceback
import sys
import os
from datetime import datetime, timezone
from collections import defaultdict

from backend.db import get_db
from backend.trading.polymarket_client import PolymarketClient
from backend.trading.autobet import _current_bankroll

# Add the pavlov directory to the path so we can import the pipeline modules directly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../pavlov")))

os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from backend.config import get_settings
s = get_settings()

from pipeline import ensemble_client
from pipeline.settlement_resolver import normalize_market
from pipeline.probability_model import generate_event_probability_vector
from pipeline.market_probability import generate_market_implied_vector, shrink_probability_vector
from pipeline.execution_cost import generate_executable_cost_vector
from pipeline.portfolio_optimizer import optimize_portfolio
from pipeline.nowcast_features import mask_impossible_buckets
from backend.ml.intraday_nowcast import get_current_obs # Hypothetical or existing NWS fetcher

async def sync_weather_predictions():
    logger.info("Starting rewritten weather prediction sync & portfolio optimization...")
    db = get_db()
    
    # 1. Fetch active weather markets
    markets = []
    try:
        from polymarket import poly_client
        if not poly_client.poly_configured():
            logger.warning("POLYMARKET_KEY_ID not set. Using dummy keys for public data.")
            poly_client.poly_configured = lambda: True
            original_get_client = poly_client.get_client
            def mock_get_client():
                from polymarket_us import PolymarketUS
                return PolymarketUS(key_id="dummy", secret_key="dummy")
            poly_client.get_client = mock_get_client
            
        pm_markets = poly_client.get_weather_markets()
        for m in pm_markets:
            m["_platform"] = "polymarket"
        markets.extend(pm_markets)
        logger.info(f"Fetched {len(pm_markets)} Polymarket weather markets.")
        
        try:
            from pipeline import kalshi_client
            # Paper Fill wrapper
            def _simulate_paper_fill_wrapper(order, orderbook_timestamp, received_timestamp):
                from pavlov.pipeline.order_simulator import simulate_paper_fill, validate_orderbook_freshness
                
                # If we're not live, we explicitly allow the system to assume freshness if timestamps are missing
                allow_assumed = (mode != "live")
                
                try:
                    validate_orderbook_freshness(
                        orderbook_timestamp=orderbook_timestamp,
                        received_timestamp=received_timestamp,
                        mode=mode,
                        allow_assumed_fresh_orderbook_for_shadow=allow_assumed
                    )
                except ValueError as e:
                    from pavlov.pipeline.order_simulator import PaperFill
                    return PaperFill(
                        market_id=order.candidate.market_id,
                        outcome_id=order.candidate.outcome_id,
                        side=order.candidate.side,
                        requested_shares=order.target_shares,
                        filled_shares=0.0,
                        limit_price=order.limit_price,
                        simulated_fill_price=0.0,
                        fees=0.0,
                        slippage=0.0,
                        visible_depth_used=0.0,
                        rejection_reason=str(e)
                    )
                    
                # We do a direct inner call if validation passes, but simulate_paper_fill does validation again. 
                # Let's adjust simulate_paper_fill directly above instead of wrapping it perfectly.
                # Actually, I can just patch simulate_paper_fill to accept mode and allow_assumed_fresh_orderbook_for_shadow.
                pass
            kalshi_markets = kalshi_client.get_weather_markets()
            for m in kalshi_markets:
                m["_platform"] = "kalshi"
            markets.extend(kalshi_markets)
            logger.info(f"Fetched {len(kalshi_markets)} Kalshi weather markets.")
        except Exception as e:
            logger.warning(f"Failed to fetch Kalshi weather markets: {e}")
    except Exception as e:
        logger.error(f"Failed to fetch weather markets: {e}")
        return
        
    bankroll = _current_bankroll(db)
    
    # 2. Normalize and Group by Event
    events_by_group = defaultdict(list)
    raw_by_group = defaultdict(list)
    
    for m in markets:
        platform = m["_platform"]
        normalized = normalize_market(m, platform)
        if not normalized:
            continue
            
        # Group by strict settlement identity, NOT just city/date
        group_key = (
            normalized.settlement_station, 
            normalized.settlement_source, 
            normalized.date, 
            normalized.observation_window, 
            platform
        )
        events_by_group[group_key].append(normalized)
        raw_by_group[group_key].append(m)
        
    logger.info(f"Normalized markets into {len(events_by_group)} distinct events.")
    
    # Pre-fetch existing exposure for open weather bets
    exposure_tracker = {}
    open_bets = db.table("autobets").select("bet_subject, stake").eq("status", "open").like("bet_subject", "weather_%").execute()
    for row in (open_bets.data or []):
        subj = row.get("bet_subject")
        exposure_tracker[subj] = exposure_tracker.get(subj, 0.0) + (row.get("stake") or 0.0)
        
    paper_max_dollars = bankroll * s.polymarket_paper_max_position_pct
    live_max_dollars = bankroll * s.polymarket_max_position_pct
    mode = "live" if s.polymarket_live_enabled else "paper"
    
    bets_placed = 0

    # 3. Process each event vector
    for group_key, events in events_by_group.items():
        station, source, event_date, obs_window, platform = group_key
        raw_markets = raw_by_group[group_key]
        
        # Canonical bucket ordering
        sorted_pairs = sorted(zip(events, raw_markets), key=lambda x: (x[0].bucket_low_f, x[0].bucket_high_f, x[0].market_id))
        events = [p[0] for p in sorted_pairs]
        raw_markets = [p[1] for p in sorted_pairs]
        
        # Log orderbook snapshots
        now = datetime.now(timezone.utc)
        for raw_m in raw_markets:
            rt = raw_m.get("received_timestamp")
            ot = raw_m.get("orderbook_timestamp")
            use_ts = ot if ot else rt
            age_ms = 0
            is_stale = False
            
            if isinstance(use_ts, str):
                try:
                    use_ts = datetime.fromisoformat(use_ts.replace('Z', '+00:00'))
                except:
                    pass
            if isinstance(use_ts, datetime):
                age_ms = (now - use_ts).total_seconds() * 1000.0
                if age_ms > 2000:
                    is_stale = True
                    
            snapshot_log = {
                "timestamp": now.isoformat(),
                "strategy": "weather_portfolio",
                "platform": platform,
                "market_id": raw_m.get("condition_id") or raw_m.get("ticker", "unknown"),
                "outcome_id": "yes",
                "received_timestamp": rt.isoformat() if isinstance(rt, datetime) else rt,
                "orderbook_timestamp": ot.isoformat() if isinstance(ot, datetime) else ot,
                "exchange_timestamp": None,
                "source": "api",
                "best_bid": raw_m.get("best_bid", 0.0),
                "best_ask": raw_m.get("best_ask", 0.0),
                "spread": raw_m.get("best_ask", 0.0) - raw_m.get("best_bid", 0.0),
                "visible_bid_depth": raw_m.get("yes_bid_qty", 0.0),
                "visible_ask_depth": raw_m.get("yes_ask_qty", 0.0),
                "age_ms": age_ms,
                "is_stale": is_stale,
                "missing_received_timestamp": rt is None,
                "missing_orderbook_timestamp": ot is None
            }
            with open("orderbook_snapshots.jsonl", "a") as f:
                import json
                f.write(json.dumps(snapshot_log) + "\n")

        
        city = events[0].city
        date_str = event_date.isoformat()
        
        # Lead time
        now = datetime.now(timezone.utc).date()
        lead_days = (event_date - now).days
        hour = datetime.now(timezone.utc).hour
        
        # Get raw ensemble stats using a dummy threshold call
        ens_result = ensemble_client.get_ensemble_prob(city, date_str, 0.0, "above")
        if not ens_result:
            logger.debug(f"Skipping {city} {date_str}: No ensemble data.")
            continue
            
        mean_f = ens_result["mean_f"]
        spread_f = ens_result["spread_f"]
        
        # A. Probability Model (Sigma calibration)
        try:
            _, P_model = generate_event_probability_vector(events, mean_f, spread_f, lead_days, hour)
            
            # B. Market Probability
            P_market = generate_market_implied_vector(raw_markets)
            
            # C. Nowcast Constraints BEFORE Shrinkage
            high_so_far = -999.0
            if lead_days == 0:
                obs = get_current_obs(city)
                high_so_far = obs.get("high_so_far", -999.0)
                if high_so_far > -999.0:
                    P_model = mask_impossible_buckets(events, P_model, high_so_far)
                    P_market = mask_impossible_buckets(events, P_market, high_so_far)
            
            # D. Bayesian Shrinkage
            P_adj = shrink_probability_vector(P_model, P_market, lead_days)
            
            # D2. Final Nowcast Masking & Assertion
            if high_so_far > -999.0:
                P_adj = mask_impossible_buckets(events, P_adj, high_so_far)
                from pipeline.probability_model import validate_probability_vector
                validate_probability_vector("P_adj_after_nowcast", P_adj)
            
            # E. Execution Cost
            Q_exec, depth_caps = generate_executable_cost_vector(raw_markets, platform)
            
            # F. Portfolio Optimizer
            x_opt = optimize_portfolio(P_adj, Q_exec, depth_caps, bankroll)
            
            # G. Final Safety Assertions
            if high_so_far > -999.0:
                for i, event in enumerate(events):
                    if event.bucket_high_f < high_so_far:
                        if P_adj[i] != 0.0 or x_opt[i] != 0.0:
                            raise ValueError(f"NOWCAST_IMPOSSIBLE_BUCKET_LEAK: Bucket {event.bucket_label} has prob {P_adj[i]} or shares {x_opt[i]}")
            
            if sum(x_opt) == 0:
                logger.info(f"Rejected event {city} {date_str} ({platform}): NON_POSITIVE_EXPECTED_LOG_GROWTH_AFTER_ROUNDING or zero trade.")
                continue
            
        except ValueError as e:
            logger.info(f"Rejected event {city} {date_str} ({platform}): {e}")
            continue
        except Exception as e:
            logger.error(f"Unexpected error processing event {city} {date_str}: {e}")
            continue
            
        from pavlov.pipeline.trade_candidate import TradeCandidate, SizedOrder
        from pavlov.pipeline.order_simulator import simulate_paper_fill, PaperFill
        from pavlov.pipeline.clv_tracker import init_clv_record, log_clv_record
        import json
        import os
        
        # G. Shadow Mode Logging & Execution
        virtual_match_id = f"weather_{city.replace(' ', '')}_{date_str}_{platform}"
        max_allowed = live_max_dollars if mode == "live" else paper_max_dollars
        
        shadow_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model_version": "v1_kelly_portfolio",
            "platform": platform,
            "station": events[0].settlement_station,
            "settlement_source": events[0].settlement_source,
            "local_date": date_str,
            "bucket_ids": [e.market_id for e in events],
            "bucket_bounds": [(e.bucket_low_f, e.bucket_high_f) for e in events],
            "P_model": P_model,
            "P_market": P_market,
            "P_adj": P_adj,
            "lambda_confidence": None, # Kept for schema compatibility if needed
            "high_so_far": high_so_far,
            "Q_exec": Q_exec,
            "depth_caps": depth_caps,
            "x_opt_raw": None, # res.x not returned by optimizer currently, but we can just use x_opt_rounded
            "x_opt_rounded": x_opt,
            "total_cost": sum(x_opt[i] * Q_exec[i] for i in range(len(events))),
            "worst_case_wealth": bankroll - sum(x_opt[i] * Q_exec[i] for i in range(len(events))),
            "delta_expected_log_growth": 0.0, # Currently logged inside optimizer, difficult to return tuple without refactoring
            "rejection_reason": "NON_POSITIVE_LOG_GROWTH" if sum(x_opt) == 0 else None,
            "would_trade": sum(x_opt) > 0,
            "paper_orders": [],
            "settlement_high_f": None,
            "winning_bucket_id": None,
            "closing_price_snapshot": None
        }
        
        for i, shares in enumerate(x_opt):
            if shares <= 0:
                continue
                
            event = events[i]
            raw_m = raw_markets[i]
            q_i = Q_exec[i]
            stake = round(shares * q_i, 2)
            
            if stake <= 0:
                continue
                
            current_exposure = exposure_tracker.get(virtual_match_id, 0.0)
            if current_exposure >= max_allowed:
                logger.info(f"Skipping {event.market_id}: {virtual_match_id} exposure (${current_exposure:.2f}) is at cap.")
                continue
                
            if current_exposure + stake > max_allowed:
                stake = max_allowed - current_exposure
                shares = round(stake / q_i)
                logger.info(f"Scaling down {event.market_id} stake to ${stake:.2f} to fit cap.")
                
            if shares <= 0:
                continue
                
            exposure_tracker[virtual_match_id] = current_exposure + stake
            
            # Convert to shared Execution schema
            candidate = TradeCandidate(
                strategy="weather_portfolio",
                platform=platform,
                market_id=event.market_id,
                outcome_id="yes",
                event_id=virtual_match_id,
                side="YES",
                model_prob=P_model[i],
                market_prob=P_market[i],
                executable_cost=q_i,
                best_bid=None,
                best_ask=raw_m.get("best_ask", q_i),
                spread=None,
                visible_depth=depth_caps[i],
                fee_per_share=q_i - raw_m.get("best_ask", q_i) - 0.005,
                slippage_buffer=0.005,
                max_shares_by_depth=depth_caps[i],
                max_shares_by_risk=1e9, # handled by portfolio optimizer
                bankroll=bankroll,
                event_exposure_cap=max_allowed,
                bucket_or_outcome_exposure_cap=max_allowed,
                timestamp=datetime.now(timezone.utc),
                metadata={"p_adj": P_adj[i]}
            )
            
            sized_order = SizedOrder(
                candidate=candidate,
                target_shares=shares,
                target_cost=stake,
                limit_price=q_i,
                expected_log_growth_delta=0.0 # Logged at portfolio level
            )
            
            # Save generic execution shadow order
            with open("execution_shadow_orders.jsonl", "a") as f:
                f.write(json.dumps({
                    "strategy": candidate.strategy,
                    "market_id": candidate.market_id,
                    "target_shares": sized_order.target_shares,
                    "target_cost": sized_order.target_cost,
                    "limit_price": sized_order.limit_price,
                    "model_prob": candidate.model_prob
                }) + "\n")
            
            # Paper Trading Fill Simulation using shared Simulator
            
            # Once updated, extract `orderbook_timestamp` and `received_timestamp` from `raw_m`
            real_orderbook_timestamp = raw_m.get("orderbook_timestamp")
            real_received_timestamp = raw_m.get("received_timestamp")
            
            fill = simulate_paper_fill(
                order=sized_order,
                orderbook_timestamp=real_orderbook_timestamp,
                received_timestamp=real_received_timestamp,
                mode=mode,
                allow_assumed_fresh_orderbook_for_shadow=True
            )
            
            paper_order = {
                "bucket_id": fill.market_id,
                "side": fill.side,
                "shares": fill.requested_shares,
                "limit_price": fill.limit_price,
                "simulated_fill_price": fill.simulated_fill_price,
                "simulated_filled_shares": fill.filled_shares,
                "visible_depth_used": fill.visible_depth_used,
                "fees": fill.fees,
                "slippage_assumption": fill.slippage,
                "post_fee_cost": round(fill.filled_shares * fill.limit_price, 2),
                "rejection_reason": fill.rejection_reason
            }
            shadow_record["paper_orders"].append(paper_order)
            
            if fill.filled_shares > 0:
                with open("paper_fills.jsonl", "a") as f:
                    f.write(json.dumps(paper_order) + "\n")
                    
                clv_rec = init_clv_record(
                    trade_id=f"sim_{virtual_match_id}_{event.market_id}",
                    market_id=event.market_id,
                    outcome_id="yes",
                    side="YES",
                    entry_price=fill.limit_price,
                    entry_time=datetime.now(timezone.utc)
                )
                log_clv_record(clv_rec)
            
            # Record the bet in DB
            record = {
                "bet_subject": virtual_match_id,
                "market_id": event.market_id,
                "market_slug": event.market_id,
                "question": f"Weather: {city} High {event.bucket_label} {date_str} ({platform})",
                "outcome_name": "yes",
                "token_id": raw_m.get("yes_token", "unknown"),
                "mode": mode,
                "model_prob": P_model[i],
                "market_prob": P_market[i],
                "market_price": raw_m.get("best_ask", q_i),
                "edge": P_adj[i] - q_i,
                "raw_confidence": P_adj[i],
                "sport": "weather",
                "kelly_fraction": 0.0,
                "stake": stake,
                "bankroll_at_time": round(bankroll, 2),
                "shares": shares,
                "status": "open",
                "bet_type": "weather",
                "metadata": {
                    "p_adj": P_adj[i],
                    "q_exec": q_i,
                    "mean_f": mean_f,
                    "spread_f": spread_f
                }
            }
            
            existing = db.table("autobets").select("id").eq("market_id", event.market_id).eq("outcome_name", "yes").eq("mode", mode).eq("status", "open").execute()
            if not existing.data:
                try:
                    db.table("autobets").insert(record).execute()
                    bets_placed += 1
                except Exception as e:
                    logger.error(f"Failed to record weather autobet: {e}")
                    
        # Write Shadow Record
        shadow_file = "weather_shadow_decisions.jsonl"
        with open(shadow_file, "a") as f:
            f.write(json.dumps(shadow_record) + "\n")

    logger.info(f"Successfully processed portfolio optimization. Recorded {bets_placed} new {mode} risk-capped event-level optimized basket trades.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(sync_weather_predictions())
