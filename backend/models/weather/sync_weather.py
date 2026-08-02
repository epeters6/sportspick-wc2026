"""
Sync weather model predictions to the unified model_predictions table and execute portfolio-optimized autobets.
Revised architecture using Bayesian probability shrinkage, full event vectors, and multi-outcome Kelly optimization.
"""
from loguru import logger
import traceback
import sys
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Running as `python backend/models/weather/sync_weather.py` puts this file's
# directory on sys.path[0], NOT the repo root — so `import backend` fails unless
# we bootstrap. (CI used to swallow that failure via check=False.)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.db import get_db
from backend.trading.polymarket_client import PolymarketClient
from backend.trading.autobet import _current_bankroll

# Weather modules live in pavlov/pipeline. ML consensus loads mlb_quant_legacy first,
# which inserts pavlov/pavlov-mlb-bot and binds `pipeline` to that package (no
# settlement_resolver). Always prefer pavlov/ and clear a poisoned import.
_PAVLOV_ROOT = os.path.join(_REPO_ROOT, "pavlov")
_WEATHER_PIPELINE = os.path.join(_PAVLOV_ROOT, "pipeline")


def _ensure_weather_pipeline_importable() -> None:
    if _PAVLOV_ROOT in sys.path:
        sys.path.remove(_PAVLOV_ROOT)
    sys.path.insert(0, _PAVLOV_ROOT)

    weather_key = os.path.normcase(os.path.abspath(_WEATHER_PIPELINE))
    for name in list(sys.modules):
        if name != "pipeline" and not name.startswith("pipeline."):
            continue
        mod = sys.modules.get(name)
        if mod is None:
            continue
        locations = []
        mod_file = getattr(mod, "__file__", None)
        if mod_file:
            locations.append(os.path.normcase(os.path.abspath(mod_file)))
        for p in getattr(mod, "__path__", []) or []:
            locations.append(os.path.normcase(os.path.abspath(p)))
        if any(loc == weather_key or loc.startswith(weather_key + os.sep) for loc in locations):
            continue
        del sys.modules[name]


_ensure_weather_pipeline_importable()

os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
from backend.config import get_settings
s = get_settings()

from pavlov.pipeline import ensemble_client
from pavlov.pipeline.settlement_resolver import normalize_market
from pavlov.pipeline.probability_model import generate_event_probability_vector
from pavlov.pipeline.market_probability import generate_market_implied_vector, shrink_probability_vector
from pavlov.pipeline.execution_cost import generate_executable_cost_vector, _as_probability
from pavlov.pipeline.portfolio_optimizer import optimize_portfolio
from pavlov.pipeline.nowcast_features import mask_impossible_buckets
from backend.ml.intraday_nowcast import get_current_obs # Hypothetical or existing NWS fetcher


def weather_candidate_id(
    platform: str,
    station: str,
    date_str: str,
    metric: str,
    market_id: str,
    mode: str,
) -> str:
    """Deterministic id shared by shadow / fill / CLV / autobet metadata."""
    return f"{platform}:{station}:{date_str}:{metric}:{market_id}:yes:{mode}"


def init_weather_clv_record(
    *,
    candidate_id: str,
    market_id: str,
    raw_m: dict,
    fill,
    platform: str,
    due_close: datetime | None = None,
    metadata: dict | None = None,
):
    """
    Persist weather CLV with market fill vs effective cost split.

    outcome_id uses the Polymarket YES token when present so book lookups work.
    entry_market_price = simulated_fill_price; entry_effective_cost = limit_price.
    """
    from pavlov.pipeline.clv_tracker import init_clv_record

    outcome_id = str(raw_m.get("yes_token") or "yes")
    return init_clv_record(
        trade_id=candidate_id,
        market_id=market_id,
        outcome_id=outcome_id,
        side="YES",
        entry_price=fill.simulated_fill_price,
        entry_market_price=fill.simulated_fill_price,
        entry_effective_cost=fill.limit_price,
        entry_time=datetime.now(timezone.utc),
        platform=platform,
        due_close=due_close,
        metadata=metadata,
    )


def _weather_market_close(raw_market: dict) -> datetime | None:
    """Return an aware UTC venue close time when the market supplies one."""
    value = (
        raw_market.get("close_time")
        or raw_market.get("endDate")
        or raw_market.get("end_date")
        or raw_market.get("expiration_time")
    )
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _append_weather_shadow_record(record: dict) -> None:
    import json

    with open("weather_shadow_decisions.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _record_weather_rejection(stats: dict, reason: str, category: str) -> None:
    stats["kelly_reject"] += 1  # Backward-compatible aggregate.
    stats[category] = stats.get(category, 0) + 1
    reasons = stats.setdefault("rejection_reasons", {})
    reasons[reason] = reasons.get(reason, 0) + 1


def _refresh_weather_orderbook(raw_market: dict, platform: str) -> dict | None:
    """Fetch a just-in-time executable book before simulating a fill."""
    market_id = str(raw_market.get("ticker") or raw_market.get("poly_market_slug") or "")
    if not market_id:
        return None
    if platform == "kalshi":
        from pavlov.pipeline import kalshi_client

        return kalshi_client.get_orderbook_as_parsed(market_id)
    if platform == "polymarket":
        from polymarket import poly_client

        return poly_client.get_orderbook_as_parsed(market_id)
    return None


async def sync_weather_predictions():
    logger.info("Starting rewritten weather prediction sync & portfolio optimization...")
    db = get_db()
    stats = {
        "polymarket_markets": 0,
        "kalshi_markets": 0,
        "events": 0,
        "ensemble_ok": 0,
        "ensemble_fail": 0,
        "kelly_reject": 0,
        "depth_reject": 0,
        "optimizer_reject": 0,
        "book_refresh_reject": 0,
        "rejection_reasons": {},
        "bets_placed": 0,
        "skipped_past": 0,
    }
    
    # 1. Fetch active weather markets (platforms isolated — one failure must not
    # skip the other; Kalshi is the primary paper-trading venue today).
    markets = []
    try:
        from polymarket import poly_client
        if not poly_client.poly_configured():
            logger.warning("POLYMARKET_KEY_ID not set. Using dummy keys for public data.")
            poly_client.poly_configured = lambda: True
            def mock_get_client():
                from polymarket_us import PolymarketUS
                return PolymarketUS(key_id="dummy", secret_key="dummy")
            poly_client.get_client = mock_get_client

        pm_markets = poly_client.get_weather_markets()
        for m in pm_markets:
            m["_platform"] = "polymarket"
        markets.extend(pm_markets)
        stats["polymarket_markets"] = len(pm_markets)
        logger.info(f"Fetched {len(pm_markets)} Polymarket weather markets.")
    except Exception as e:
        logger.warning(f"Failed to fetch Polymarket weather markets: {e}")

    try:
        _ensure_weather_pipeline_importable()
        from pavlov.pipeline import kalshi_client
        kalshi_markets = kalshi_client.get_weather_markets()
        for m in kalshi_markets:
            m["_platform"] = "kalshi"
        markets.extend(kalshi_markets)
        stats["kalshi_markets"] = len(kalshi_markets)
        logger.info(f"Fetched {len(kalshi_markets)} Kalshi weather markets.")
    except Exception as e:
        logger.warning(f"Failed to fetch Kalshi weather markets: {e}")

    if not markets:
        # Raise so run_sync prints WEATHER SYNC FAILED (silent return kept CI green
        # with empty artifacts and zero paper bets).
        msg = (
            "No weather markets fetched from Polymarket or Kalshi — aborting weather sync. "
            f"poly={stats['polymarket_markets']} kalshi={stats['kalshi_markets']}"
        )
        logger.error(msg)
        _write_weather_sync_status(ok=False, stats=stats, error=msg)
        raise RuntimeError(msg)
        
    bankroll = _current_bankroll(db)
    
    # 2. Normalize and Group by Event
    events_by_group = defaultdict(list)
    raw_by_group = defaultdict(list)
    
    for m in markets:
        platform = m["_platform"]
        normalized = normalize_market(m, platform)
        if not normalized:
            continue
            
        # Group by strict settlement identity, NOT just city/date.
        # metric matters: HIGH and LOW buckets for the same station/date are
        # separate mutually-exclusive event spaces.
        group_key = (
            normalized.settlement_station, 
            normalized.settlement_source, 
            normalized.date, 
            normalized.observation_window, 
            platform,
            normalized.metric
        )
        events_by_group[group_key].append(normalized)
        raw_by_group[group_key].append(m)
        
    stats["events"] = len(events_by_group)
    logger.info(f"Normalized markets into {len(events_by_group)} distinct events.")
    if not events_by_group:
        msg = "Weather markets fetched but none normalized into events."
        logger.error(msg)
        _write_weather_sync_status(ok=False, stats=stats, error=msg)
        raise RuntimeError(msg)
    
    # Pre-fetch exposure + open-position keys for duplicate detection before fills
    import json as _json
    exposure_tracker = {}
    open_candidate_ids: set[str] = set()
    open_legacy_keys: set[str] = set()
    open_bets = (
        db.table("autobets")
        .select("bet_subject, stake, market_id, outcome_name, mode, metadata")
        .eq("status", "open")
        .like("bet_subject", "weather_%")
        .execute()
    )
    for row in (open_bets.data or []):
        subj = row.get("bet_subject")
        exposure_tracker[subj] = exposure_tracker.get(subj, 0.0) + (row.get("stake") or 0.0)
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        if isinstance(meta, dict) and meta.get("candidate_id"):
            open_candidate_ids.add(str(meta["candidate_id"]))
        mid = row.get("market_id")
        out = str(row.get("outcome_name") or "yes").lower()
        row_mode = row.get("mode") or "paper"
        if mid:
            open_legacy_keys.add(f"{mid}:{out}:{row_mode}")

    paper_max_dollars = bankroll * s.polymarket_paper_max_position_pct
    live_max_dollars = bankroll * s.polymarket_max_position_pct
    from backend.trading.live_toggle import is_live_mode
    mode = "live" if is_live_mode(s, db) else "paper"
    
    bets_placed = 0

    # 3. Process each event vector
    for group_key, events in events_by_group.items():
        station, source, event_date, obs_window, platform, metric = group_key
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

            snap_bid = _as_probability(
                raw_m.get("best_bid", raw_m.get("yes_bid"))
            )
            snap_ask = _as_probability(
                raw_m.get("best_ask", raw_m.get("yes_ask"))
            )
            bid_depth = float(
                raw_m.get("yes_bid_size")
                or raw_m.get("yes_bid_qty")
                or 0.0
            )
            ask_depth = float(
                raw_m.get("yes_ask_size")
                or raw_m.get("yes_ask_qty")
                or raw_m.get("ask_size")
                or 0.0
            )
                    
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
                "best_bid": snap_bid,
                "best_ask": snap_ask,
                "spread": snap_ask - snap_bid,
                "visible_bid_depth": bid_depth,
                "visible_ask_depth": ask_depth,
                "age_ms": age_ms,
                "is_stale": is_stale,
                "missing_received_timestamp": rt is None,
                "missing_orderbook_timestamp": ot is None
            }
            with open("orderbook_snapshots.jsonl", "a") as f:
                import json
                f.write(json.dumps(snapshot_log, default=str) + "\n")

        
        city = events[0].city
        date_str = event_date.isoformat()
        
        # Lead time in STATION-LOCAL time — UTC date/hour is wrong for US
        # evening settlement (e.g. 01:00 UTC is still "today" in Phoenix).
        from zoneinfo import ZoneInfo
        from pavlov.pipeline.station_mapper import get_tz_for_city
        local_now = datetime.now(ZoneInfo(get_tz_for_city(city)))
        lead_days = (event_date - local_now.date()).days
        hour = local_now.hour

        # Past local dates cannot settle as open markets we still want to trade;
        # Open-Meteo also drops them from the forecast window.
        if lead_days < 0:
            stats["skipped_past"] += 1
            logger.info(f"Skipping {city} {date_str} ({metric}): past local date (lead={lead_days}).")
            continue
        
        # Get raw ensemble stats using a dummy threshold call (metric-aware:
        # LOW markets need the daily-minimum ensemble members, not the maximum)
        ens_result = ensemble_client.get_ensemble_prob(city, date_str, 0.0, "above", metric=metric)
        if not ens_result:
            stats["ensemble_fail"] += 1
            # INFO so CI logs show empty-ensemble / past-date skips (was silent at DEBUG).
            logger.info(f"Skipping {city} {date_str} ({metric}): No ensemble data.")
            continue
        stats["ensemble_ok"] += 1
            
        mean_f = ens_result["mean_f"]
        spread_f = ens_result["spread_f"]
        
        # Record the raw (pre-MOS) forecast so the verification loop can grade it later
        try:
            from backend.ml.weather_verification import record_prediction
            record_prediction(
                events[0].settlement_station, max(lead_days, 0), date_str,
                mean_f, metric=metric, model_name="ensemble",
            )
        except Exception as exc:
            logger.debug(f"Verification record failed for {city} {date_str}: {exc}")
        
        # A. Probability Model (Sigma calibration + MOS bias correction)
        mos_bias = 0.0
        try:
            from backend.ml.weather_mos import mos_engine
            mos_bias = mos_engine.calculate_bias(
                events[0].settlement_station, "ensemble", max(lead_days, 0), metric
            )
        except Exception as exc:
            logger.debug(f"MOS bias unavailable for {city} (using 0.0): {exc}")
        P_model: list[float] = []
        P_market: list[float] = []
        P_adj: list[float] = []
        Q_exec: list[float] = []
        depth_caps: list[float] = []
        x_opt: list[float] = []
        observed_extreme = -999.0 if metric == "high" else 999.0
        nowcast_active = False
        shadow_record: dict | None = None
        try:
            _, P_model = generate_event_probability_vector(events, mean_f, spread_f, lead_days, hour, bias_correction=mos_bias)
            
            # B. Market Probability
            P_market = generate_market_implied_vector(raw_markets)
            
            # C. Nowcast Constraints BEFORE Shrinkage
            # For HIGH markets the running max rules out low buckets; for LOW
            # markets the running min rules out high buckets.
            if lead_days == 0:
                obs = get_current_obs(city)
                if metric == "high":
                    observed_extreme = obs.get("high_so_far", -999.0)
                    nowcast_active = observed_extreme > -999.0
                else:
                    observed_extreme = obs.get("low_so_far", 999.0)
                    nowcast_active = observed_extreme < 999.0
                if nowcast_active:
                    P_model = mask_impossible_buckets(events, P_model, observed_extreme, metric=metric)
                    P_market = mask_impossible_buckets(events, P_market, observed_extreme, metric=metric)
            
            # D. Bayesian Shrinkage
            P_adj = shrink_probability_vector(P_model, P_market, lead_days)
            
            # D2. Final Nowcast Masking & Assertion
            if nowcast_active:
                P_adj = mask_impossible_buckets(events, P_adj, observed_extreme, metric=metric)
                from pavlov.pipeline.probability_model import validate_probability_vector
                validate_probability_vector("P_adj_after_nowcast", P_adj)
            
            # E. Execution Cost
            Q_exec, depth_caps = generate_executable_cost_vector(raw_markets, platform)
            
            # F. Portfolio Optimizer
            x_opt = optimize_portfolio(P_adj, Q_exec, depth_caps, bankroll)

            shadow_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model_version": "v1_kelly_portfolio",
                "platform": platform,
                "station": events[0].settlement_station,
                "settlement_source": events[0].settlement_source,
                "local_date": date_str,
                "bucket_ids": [e.market_id for e in events],
                "bucket_bounds": [
                    (e.bucket_low_f, e.bucket_high_f) for e in events
                ],
                "P_model": P_model,
                "P_market": P_market,
                "P_adj": P_adj,
                "lambda_confidence": None,
                "metric": metric,
                "observed_extreme_so_far": (
                    observed_extreme if nowcast_active else None
                ),
                "Q_exec": Q_exec,
                "depth_caps": depth_caps,
                "x_opt_raw": None,
                "x_opt_rounded": x_opt,
                "total_cost": sum(
                    x_opt[i] * Q_exec[i] for i in range(len(events))
                ),
                "worst_case_wealth": bankroll
                - sum(x_opt[i] * Q_exec[i] for i in range(len(events))),
                "delta_expected_log_growth": 0.0,
                "rejection_reason": None,
                "would_trade": sum(x_opt) > 0,
                "paper_orders": [],
                "settlement_high_f": None,
                "winning_bucket_id": None,
                "closing_price_snapshot": None,
            }
            
            # G. Final Safety Assertions
            if nowcast_active:
                for i, event in enumerate(events):
                    impossible = (
                        event.bucket_high_f < observed_extreme
                        if metric == "high"
                        else event.bucket_low_f > observed_extreme
                    )
                    if impossible:
                        if P_adj[i] != 0.0 or x_opt[i] != 0.0:
                            raise ValueError(f"NOWCAST_IMPOSSIBLE_BUCKET_LEAK: Bucket {event.bucket_label} has prob {P_adj[i]} or shares {x_opt[i]}")
            
            if sum(x_opt) == 0:
                positive_edge_indexes = [
                    i
                    for i in range(len(events))
                    if P_adj[i] - Q_exec[i] >= 0.015
                ]
                if not any(depth_caps):
                    reason = "MISSING_EXECUTABLE_DEPTH"
                    category = "depth_reject"
                elif positive_edge_indexes and not any(
                    depth_caps[i] > 0 for i in positive_edge_indexes
                ):
                    reason = "INSUFFICIENT_DEPTH_AT_POSITIVE_EDGE"
                    category = "depth_reject"
                else:
                    reason = "NON_POSITIVE_EXPECTED_LOG_GROWTH_AFTER_ROUNDING"
                    category = "optimizer_reject"
                _record_weather_rejection(stats, reason, category)
                shadow_record["rejection_reason"] = reason
                _append_weather_shadow_record(shadow_record)
                logger.info(
                    f"Rejected event {city} {date_str} ({platform}): {reason}"
                )
                continue
            
        except ValueError as e:
            reason = str(e)
            _record_weather_rejection(stats, reason, "optimizer_reject")
            _append_weather_shadow_record(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "model_version": "v1_kelly_portfolio",
                    "platform": platform,
                    "station": events[0].settlement_station,
                    "settlement_source": events[0].settlement_source,
                    "local_date": date_str,
                    "bucket_ids": [event.market_id for event in events],
                    "bucket_bounds": [
                        (event.bucket_low_f, event.bucket_high_f)
                        for event in events
                    ],
                    "P_model": P_model,
                    "P_market": P_market,
                    "P_adj": P_adj,
                    "Q_exec": Q_exec,
                    "depth_caps": depth_caps,
                    "x_opt_rounded": x_opt,
                    "metric": metric,
                    "rejection_reason": reason,
                    "would_trade": False,
                    "paper_orders": [],
                }
            )
            logger.info(f"Rejected event {city} {date_str} ({platform}): {reason}")
            continue
        except Exception as e:
            logger.error(f"Unexpected error processing event {city} {date_str}: {e}")
            continue
            
        from pavlov.pipeline.trade_candidate import TradeCandidate, SizedOrder
        from pavlov.pipeline.order_simulator import simulate_paper_fill, PaperFill
        from pavlov.pipeline.clv_tracker import log_clv_record
        import json
        import os
        
        # G. Shadow Mode Logging & Execution
        metric_tag = "" if metric == "high" else "_low"
        virtual_match_id = f"weather_{city.replace(' ', '')}_{date_str}{metric_tag}_{platform}"
        max_allowed = live_max_dollars if mode == "live" else paper_max_dollars
        
        assert shadow_record is not None
        
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

            station_id = events[0].settlement_station
            candidate_id = weather_candidate_id(
                platform, station_id, date_str, metric, event.market_id, mode
            )
            legacy_key = f"{event.market_id}:yes:{mode}"
            # Duplicate check BEFORE fill / CLV / PaperFill so artifacts match DB
            if candidate_id in open_candidate_ids or legacy_key in open_legacy_keys:
                logger.info(
                    f"Decision rejection DUPLICATE_OPEN_POSITION candidate_id={candidate_id}"
                )
                shadow_record.setdefault("rejections", []).append(
                    {"candidate_id": candidate_id, "reason": "DUPLICATE_OPEN_POSITION"}
                )
                shadow_record["paper_orders"].append({
                    "candidate_id": candidate_id,
                    "bucket_id": event.market_id,
                    "side": "YES",
                    "shares": shares,
                    "rejection_reason": "DUPLICATE_OPEN_POSITION",
                })
                if not shadow_record.get("rejection_reason"):
                    shadow_record["rejection_reason"] = "DUPLICATE_OPEN_POSITION"
                continue

            fresh_book = _refresh_weather_orderbook(raw_m, platform)
            fresh_ask = _as_probability(
                (fresh_book or {}).get(
                    "best_ask", (fresh_book or {}).get("yes_ask")
                )
            )
            fresh_depth = float(
                (fresh_book or {}).get("ask_size")
                or (fresh_book or {}).get("yes_ask_size")
                or (fresh_book or {}).get("yes_ask_qty")
                or 0.0
            )
            fresh_received = (fresh_book or {}).get("received_timestamp")
            fresh_orderbook_ts = (fresh_book or {}).get("orderbook_timestamp")
            if (
                not fresh_book
                or fresh_ask <= 0.0
                or fresh_ask >= 1.0
                or fresh_depth <= 0.0
                or fresh_received is None
            ):
                reason = "WINNING_BOOK_REFRESH_FAILED"
                stats["book_refresh_reject"] += 1
                reasons = stats.setdefault("rejection_reasons", {})
                reasons[reason] = reasons.get(reason, 0) + 1
                shadow_record.setdefault("rejections", []).append(
                    {"candidate_id": candidate_id, "reason": reason}
                )
                shadow_record["paper_orders"].append(
                    {
                        "candidate_id": candidate_id,
                        "bucket_id": event.market_id,
                        "side": "YES",
                        "shares": shares,
                        "rejection_reason": reason,
                    }
                )
                shadow_record["rejection_reason"] = (
                    shadow_record.get("rejection_reason") or reason
                )
                logger.info(
                    f"Paper fill rejected for {event.market_id}: {reason}"
                )
                continue

            fresh_costs, fresh_caps = generate_executable_cost_vector(
                [fresh_book], platform
            )
            fresh_q = fresh_costs[0]
            if fresh_q > q_i + 0.005:
                reason = "PRICE_MOVED_AGAINST_US"
            elif P_adj[i] - fresh_q < 0.015:
                reason = "EDGE_GONE_AFTER_REPRICE"
            else:
                reason = ""
            if reason:
                stats["book_refresh_reject"] += 1
                reasons = stats.setdefault("rejection_reasons", {})
                reasons[reason] = reasons.get(reason, 0) + 1
                shadow_record.setdefault("rejections", []).append(
                    {"candidate_id": candidate_id, "reason": reason}
                )
                shadow_record["paper_orders"].append(
                    {
                        "candidate_id": candidate_id,
                        "bucket_id": event.market_id,
                        "side": "YES",
                        "shares": shares,
                        "rejection_reason": reason,
                    }
                )
                shadow_record["rejection_reason"] = (
                    shadow_record.get("rejection_reason") or reason
                )
                continue

            raw_m.update(fresh_book)
            q_i = fresh_q
            depth_caps[i] = fresh_caps[0]
            shares = min(shares, depth_caps[i])
            stake = round(shares * q_i, 2)
            if shares <= 0 or stake <= 0:
                continue
            current_exposure = exposure_tracker.get(virtual_match_id, 0.0)
            if current_exposure + stake > max_allowed:
                remaining = max(0.0, max_allowed - current_exposure)
                shares = min(shares, int(remaining / q_i))
                stake = round(shares * q_i, 2)
            if shares <= 0 or stake <= 0:
                continue

            exposure_tracker[virtual_match_id] = current_exposure + stake
            
            # Convert to shared Execution schema
            best_ask_p = _as_probability(raw_m.get("best_ask", raw_m.get("yes_ask", q_i))) or q_i
            candidate = TradeCandidate(
                strategy="weather_portfolio",
                platform=platform,
                market_id=event.market_id,
                outcome_id="yes",
                event_id=virtual_match_id,
                side="YES",
                # P_adj is the probability that actually passed selection after
                # market shrinkage. Keep the raw ensemble probability in metadata.
                model_prob=P_adj[i],
                market_prob=P_market[i],
                executable_cost=q_i,
                best_bid=None,
                best_ask=best_ask_p,
                spread=None,
                visible_depth=depth_caps[i],
                fee_per_share=max(0.0, q_i - best_ask_p - 0.005),
                slippage_buffer=0.005,
                max_shares_by_depth=depth_caps[i],
                max_shares_by_risk=1e9, # handled by portfolio optimizer
                bankroll=bankroll,
                event_exposure_cap=max_allowed,
                bucket_or_outcome_exposure_cap=max_allowed,
                timestamp=datetime.now(timezone.utc),
                metadata={
                    "p_adj": P_adj[i],
                    "raw_model_prob": P_model[i],
                    "candidate_id": candidate_id,
                }
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
                    "candidate_id": candidate_id,
                    "market_id": candidate.market_id,
                    "target_shares": sized_order.target_shares,
                    "target_cost": sized_order.target_cost,
                    "limit_price": sized_order.limit_price,
                    "model_prob": candidate.model_prob
                }) + "\n")
            
            # Paper Trading Fill Simulation using shared Simulator
            
            # Once updated, extract `orderbook_timestamp` and `received_timestamp` from `raw_m`
            real_orderbook_timestamp = fresh_orderbook_ts
            real_received_timestamp = fresh_received
            
            fill = simulate_paper_fill(
                order=sized_order,
                orderbook_timestamp=real_orderbook_timestamp,
                received_timestamp=real_received_timestamp,
                mode=mode,
                allow_received_timestamp_for_shadow=(
                    platform == "kalshi" and mode != "live"
                ),
            )
            
            paper_order = {
                "candidate_id": candidate_id,
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
            
            if fill.filled_shares <= 0:
                logger.info(
                    f"Paper fill rejected for {event.market_id}: "
                    f"{fill.rejection_reason or 'unknown'}"
                )
                # Undo exposure reservation so a later retry can try again.
                exposure_tracker[virtual_match_id] = max(
                    0.0, exposure_tracker.get(virtual_match_id, 0.0) - stake
                )
                continue

            with open("paper_fills.jsonl", "a") as f:
                f.write(json.dumps(paper_order) + "\n")

            close_time = _weather_market_close(raw_m)
            filled_stake = round(fill.filled_shares * fill.limit_price, 2)
            clv_metadata = {
                "event_id": virtual_match_id,
                "event_start": close_time.isoformat() if close_time else None,
                "event_start_utc": close_time.isoformat() if close_time else None,
                "close_lead_minutes": 5,
                "model_prob": P_adj[i],
                "raw_model_prob": P_model[i],
                "market_prob": fill.simulated_fill_price,
                "market_vector_prob": P_market[i],
                "shares": fill.filled_shares,
                "stake": filled_stake,
                "station": station_id,
                "metric": metric,
                "target_date": date_str,
            }
            clv_rec = init_weather_clv_record(
                candidate_id=candidate_id,
                market_id=event.market_id,
                raw_m=raw_m,
                fill=fill,
                platform=platform,
                due_close=(close_time - timedelta(minutes=5)) if close_time else None,
                metadata=clv_metadata,
            )
            log_clv_record(clv_rec)

            # Record only filled paper/live bets in DB
            record = {
                "venue": platform,
                "bet_subject": virtual_match_id,
                "market_id": event.market_id,
                "market_slug": event.market_id,
                "question": f"Weather: {city} {'High' if metric == 'high' else 'Low'} {event.bucket_label} {date_str} ({platform})",
                "outcome_name": "yes",
                "token_id": raw_m.get("yes_token", "unknown"),
                "mode": mode,
                # Store the probability used for the decision. The raw ensemble
                # value remains available separately for calibration analysis.
                "model_prob": P_adj[i],
                "market_prob": P_market[i],
                "market_price": best_ask_p,
                "edge": P_adj[i] - q_i,
                "raw_confidence": P_model[i],
                "sport": "weather",
                "event_date": date_str,
                "strategy": f"weather_{metric}",
                # Effective fraction implied by the portfolio optimizer's sizing
                "kelly_fraction": round(stake / bankroll, 4) if bankroll > 0 else 0.0,
                "stake": stake,
                "bankroll_at_time": round(bankroll, 2),
                "shares": shares,
                "status": "open",
                "bet_type": "weather",
                "metadata": {
                    "candidate_id": candidate_id,
                    "p_adj": P_adj[i],
                    "raw_model_prob": P_model[i],
                    "market_vector_prob": P_market[i],
                    "q_exec": q_i,
                    "mean_f": mean_f,
                    "spread_f": spread_f,
                    # Everything settlement needs to grade this bet against
                    # observed temps if the exchange never reports resolution
                    "metric": metric,
                    "station": station_id,
                    "city": city,
                    "target_date": date_str,
                    "bucket_low_f": event.bucket_low_f if event.bucket_low_f != float("-inf") else None,
                    "bucket_high_f": event.bucket_high_f if event.bucket_high_f != float("inf") else None,
                    "bucket_label": event.bucket_label,
                    "mos_bias": mos_bias
                }
            }
            
            try:
                db.table("autobets").insert(record).execute()
                bets_placed += 1
                open_candidate_ids.add(candidate_id)
                open_legacy_keys.add(legacy_key)
            except Exception as e:
                # Retry without optional columns when migrations are pending
                msg = str(e)
                slim = dict(record)
                for col in (
                    "metadata",
                    "raw_confidence",
                    "bet_type",
                    "sport",
                    "venue",
                    "event_date",
                    "strategy",
                ):
                    if col in msg or "PGRST204" in msg or "schema cache" in msg:
                        slim.pop(col, None)
                try:
                    db.table("autobets").insert(slim).execute()
                    bets_placed += 1
                    open_candidate_ids.add(candidate_id)
                    open_legacy_keys.add(legacy_key)
                    logger.warning(f"Weather autobet recorded with slim schema ({e})")
                except Exception as e2:
                    logger.error(f"Failed to record weather autobet: {e2}")
                    exposure_tracker[virtual_match_id] = max(
                        0.0, exposure_tracker.get(virtual_match_id, 0.0) - stake
                    )
        _append_weather_shadow_record(shadow_record)

    stats["bets_placed"] = bets_placed
    logger.info(
        f"Weather sync summary: markets poly={stats['polymarket_markets']} "
        f"kalshi={stats['kalshi_markets']} events={stats['events']} "
        f"ensemble_ok={stats['ensemble_ok']} ensemble_fail={stats['ensemble_fail']} "
        f"kelly_reject={stats['kelly_reject']} depth_reject={stats['depth_reject']} "
        f"optimizer_reject={stats['optimizer_reject']} "
        f"book_refresh_reject={stats['book_refresh_reject']} "
        f"skipped_past={stats['skipped_past']} "
        f"bets_placed={bets_placed} mode={mode}"
    )
    logger.info(f"Successfully processed portfolio optimization. Recorded {bets_placed} new {mode} risk-capped event-level optimized basket trades.")
    _write_weather_sync_status(ok=True, stats=stats, error=None)
    if stats["ensemble_ok"] == 0 and stats["events"] > 0:
        msg = (
            f"Weather sync ran but ensemble returned data for 0/{stats['events']} events "
            f"(fail={stats['ensemble_fail']}, past={stats['skipped_past']})."
        )
        logger.error(msg)
        raise RuntimeError(msg)


def _write_weather_sync_status(*, ok: bool, stats: dict, error: str | None) -> None:
    import json
    payload = {
        "ok": ok,
        "error": error,
        "stats": stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open("weather_sync_status.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        logger.warning(f"Could not write weather_sync_status.json: {exc}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(sync_weather_predictions())
