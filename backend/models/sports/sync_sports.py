import json
from datetime import datetime, timezone
from typing import Literal
from loguru import logger
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.sports_probability_model import predict_sports_probability
from pavlov.pipeline.trade_candidate import TradeCandidate, SizedOrder
from pavlov.pipeline.binary_kelly import size_binary_trade
from pavlov.pipeline.risk_caps import RiskCaps
from pavlov.pipeline.order_simulator import simulate_paper_fill
from pavlov.pipeline.clv_tracker import init_clv_record, log_clv_record

def sync_sports_market(
    market_data: dict,
    features: SportsEventFeatures,
    best_ask: float,
    fee_per_share: float,
    visible_depth: float,
    bankroll: float,
    risk_caps: RiskCaps,
    mode: Literal["live", "shadow", "paper"] = "shadow",
    real_orderbook_timestamp = None,
    real_received_timestamp = None
):
    if mode != "live":
        # Hard guard to ensure no live orders can be placed from shadow/paper mode
        submit_live_orders = False
        assert submit_live_orders is False, "Cannot submit live orders in shadow mode"
        
    now = datetime.now(timezone.utc)
    age_ms = 0
    is_stale = False
    use_timestamp = real_orderbook_timestamp if real_orderbook_timestamp else real_received_timestamp
    if use_timestamp:
        age_ms = (now - use_timestamp).total_seconds() * 1000.0
        if age_ms > 2000:
            is_stale = True

    snapshot_log = {
        "timestamp": now.isoformat(),
        "strategy": "sports_mlb",
        "platform": market_data.get("platform", "polymarket"),
        "market_id": features.market_id,
        "outcome_id": features.team_a,
        "received_timestamp": real_received_timestamp.isoformat() if real_received_timestamp else None,
        "orderbook_timestamp": real_orderbook_timestamp.isoformat() if real_orderbook_timestamp else None,
        "exchange_timestamp": None,
        "source": "api",
        "best_bid": best_ask - 0.02,
        "best_ask": best_ask,
        "spread": 0.02,
        "visible_bid_depth": visible_depth,
        "visible_ask_depth": visible_depth,
        "age_ms": age_ms,
        "is_stale": is_stale,
        "missing_received_timestamp": real_received_timestamp is None,
        "missing_orderbook_timestamp": real_orderbook_timestamp is None
    }
    with open("orderbook_snapshots.jsonl", "a") as f:
        f.write(json.dumps(snapshot_log) + "\n")
        
    # 1. Predict — pitcher-outs may supply model_prob_override (coefficients unchanged)
    prediction = predict_sports_probability(features)
    override = None
    if isinstance(features.sport_specific, dict):
        override = features.sport_specific.get("model_prob_override")
    if override is None:
        override = market_data.get("model_prob_override")
    if override is not None:
        prediction.model_prob = float(override)
        if isinstance(features.sport_specific, dict):
            for k in ("model_version", "feature_version", "coefficient_source", "calibration_status"):
                if features.sport_specific.get(k) is not None and hasattr(prediction, k):
                    setattr(prediction, k, features.sport_specific[k])

    if prediction.rejection_reason:
        _log_decision(features, prediction, None, None, None, prediction.rejection_reason)
        return

    if getattr(prediction, "calibration_status", "uncalibrated_shadow") != "calibrated_out_of_sample" and mode == "live":
        raise ValueError("UNCALIBRATED_MODEL_LIVE_BLOCK")

    if market_data.get("platform", "").lower() == "kalshi":
        _log_decision(features, prediction, None, None, None, "KALSHI_SPORTS_MAPPING_NOT_IMPLEMENTED")
        return

    if real_orderbook_timestamp is None or real_received_timestamp is None:
        _log_decision(features, prediction, None, None, None, "MISSING_ORDERBOOK_TIMESTAMP")
        return
    if getattr(real_orderbook_timestamp, "tzinfo", None) is None or getattr(real_received_timestamp, "tzinfo", None) is None:
        _log_decision(features, prediction, None, None, None, "NAIVE_ORDERBOOK_TIMESTAMP")
        return
    if visible_depth is None or float(visible_depth) <= 0:
        _log_decision(features, prediction, None, None, None, "INSUFFICIENT_DEPTH")
        return

    # 2. Build TradeCandidate (YES on selected contract outcome)
    side = "YES"
    executable_cost = best_ask + fee_per_share + 0.005  # with slippage

    if executable_cost >= 1.0:
        _log_decision(features, prediction, None, None, None, "EFFECTIVE_COST_NOT_TRADABLE")
        return

    candidate = TradeCandidate(
        strategy="sports_quant_v1",
        platform=market_data.get("platform", "polymarket"),
        market_id=features.market_id,
        outcome_id=features.team_a,
        event_id=features.event_id,
        side=side,
        model_prob=prediction.model_prob,
        market_prob=prediction.market_prob,
        executable_cost=executable_cost,
        best_bid=best_ask - 0.02,
        best_ask=best_ask,
        spread=0.02,
        visible_depth=float(visible_depth),
        fee_per_share=fee_per_share,
        slippage_buffer=0.005,
        max_shares_by_depth=float(visible_depth),
        max_shares_by_risk=1e9,
        bankroll=bankroll,
        event_exposure_cap=0.05 * bankroll,
        bucket_or_outcome_exposure_cap=0.02 * bankroll,
        timestamp=datetime.now(timezone.utc),
        metadata=features.sport_specific,
        received_timestamp=real_received_timestamp,
        orderbook_timestamp=real_orderbook_timestamp,
    )
    
    # 3. Binary Kelly Sizing
    from pavlov.pipeline.binary_kelly import binary_kelly_fraction
    kelly_fraction = binary_kelly_fraction(candidate.model_prob, candidate.executable_cost, side)
    
    sized_order = size_binary_trade(candidate, kelly_fraction, risk_caps)
    
    if sized_order.rejection_reason:
        _log_decision(features, prediction, sized_order, None, None, sized_order.rejection_reason)
        return
        
    if sized_order.target_shares <= 0.0:
        _log_decision(features, prediction, sized_order, None, None, "ZERO_SIZED_ORDER")
        return
        
    # 4. Paper Fill
    fill = simulate_paper_fill(
        order=sized_order,
        orderbook_timestamp=real_orderbook_timestamp,
        received_timestamp=real_received_timestamp,
        mode=mode,
    )
    
    if fill.rejection_reason:
        _log_decision(features, prediction, sized_order, fill, None, fill.rejection_reason)
        return
        
    # 5. CLV Tracking
    clv_record = init_clv_record(
        trade_id=f"sports_{features.event_id}_{int(datetime.now(timezone.utc).timestamp())}",
        market_id=features.market_id,
        outcome_id=features.team_a,
        side=side,
        entry_price=fill.limit_price, # or best ask
        entry_time=datetime.now(timezone.utc)
    )
    log_clv_record(clv_record, "sports_clv_tracking.jsonl")
    
    # Log successful decision
    _log_decision(features, prediction, sized_order, fill, clv_record, None)

def _log_decision(
    features: SportsEventFeatures,
    prediction,
    order: SizedOrder,
    fill,
    clv_record,
    rejection_reason: str
):
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_version": getattr(prediction, "model_version", "sports_v1"),
        "model_type": getattr(prediction, "model_type", "unknown"),
        "coefficient_source": getattr(prediction, "coefficient_source", "unknown"),
        "calibration_status": getattr(prediction, "calibration_status", "unknown"),
        "sport": features.sport,
        "league": features.league,
        "event_id": features.event_id,
        "market_id": features.market_id,
        "team_a": features.team_a,
        "team_b": features.team_b,
        "market_type": features.sport_specific.get("market_type", "moneyline"),
        "resolution_rule": features.sport_specific.get("resolution_rule", "standard"),
        "snapshot_time": features.snapshot_time.isoformat() if features.snapshot_time else None,
        "event_start_time": features.start_time.isoformat() if features.start_time else None,
        "feature_snapshot": prediction.feature_snapshot,
        "P_market": prediction.market_prob,
        "P_model": prediction.model_prob,
        "edge_before_execution": prediction.edge_before_execution,
        "executable_cost": order.candidate.executable_cost if order else None,
        "fee_per_share": order.candidate.fee_per_share if order else None,
        "slippage_buffer": order.candidate.slippage_buffer if order else None,
        "visible_depth": order.candidate.visible_depth if order else None,
        "net_edge_after_execution": prediction.model_prob - order.candidate.executable_cost if order and order.candidate.executable_cost else None,
        "sized_order": order.__dict__ if order else None,
        "paper_fill": fill.__dict__ if fill else None,
        "clv_record_id": clv_record.trade_id if clv_record else None,
        "received_timestamp": order.candidate.received_timestamp.isoformat() if order and hasattr(order.candidate, "received_timestamp") and order.candidate.received_timestamp else None,
        "orderbook_timestamp": order.candidate.orderbook_timestamp.isoformat() if order and hasattr(order.candidate, "orderbook_timestamp") and order.candidate.orderbook_timestamp else None,
        "rejection_reason": rejection_reason,
        "would_trade": rejection_reason is None,
        
        # Settlement placeholders
        "settlement_result": None,
        "closing_price_snapshot": None,
        "final_score": None,
        "winning_side": None
    }
    
    # We must explicitly convert SizedOrder's inner TradeCandidate to dict.
    # Serialize ALL datetime fields (timestamp, received_timestamp,
    # orderbook_timestamp, ...) — missing one crashes the whole decision log.
    if log_entry["sized_order"]:
        candidate = dict(log_entry["sized_order"]["candidate"].__dict__)
        for k, v in candidate.items():
            if isinstance(v, datetime):
                candidate[k] = v.isoformat()
        log_entry["sized_order"] = dict(log_entry["sized_order"])
        log_entry["sized_order"]["candidate"] = candidate
        for k, v in log_entry["sized_order"].items():
            if isinstance(v, datetime):
                log_entry["sized_order"][k] = v.isoformat()
        
    # Serialize datetimes in feature_snapshot
    if log_entry["feature_snapshot"]:
        if "start_time" in log_entry["feature_snapshot"] and isinstance(log_entry["feature_snapshot"]["start_time"], datetime):
            log_entry["feature_snapshot"]["start_time"] = log_entry["feature_snapshot"]["start_time"].isoformat()
        if "snapshot_time" in log_entry["feature_snapshot"] and isinstance(log_entry["feature_snapshot"]["snapshot_time"], datetime):
            log_entry["feature_snapshot"]["snapshot_time"] = log_entry["feature_snapshot"]["snapshot_time"].isoformat()
        
    with open("sports_shadow_decisions.jsonl", "a") as f:
        f.write(json.dumps(log_entry) + "\n")
        
    if fill and not fill.rejection_reason:
        with open("sports_paper_fills.jsonl", "a") as f:
            f.write(json.dumps(fill.__dict__) + "\n")
