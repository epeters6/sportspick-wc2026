import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from loguru import logger
from pavlov.pipeline.sports_features import SportsEventFeatures
from pavlov.pipeline.sports_probability_model import predict_sports_probability
from pavlov.pipeline.trade_candidate import TradeCandidate, SizedOrder
from pavlov.pipeline.binary_kelly import size_binary_trade
from pavlov.pipeline.risk_caps import RiskCaps
from pavlov.pipeline.order_simulator import simulate_paper_fill
from pavlov.pipeline.clv_tracker import init_clv_record, log_clv_record
from pavlov.pipeline.fee_model import estimate_fee_per_share


def _sync_result(
    *,
    rejection_reason: Optional[str] = None,
    would_trade: bool = False,
    paper_filled: bool = False,
    clv_obligation_created: bool = False,
    executable_cost: Optional[float] = None,
    net_edge: Optional[float] = None,
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    out = {
        "rejection_reason": rejection_reason,
        "would_trade": would_trade,
        "paper_filled": paper_filled,
        "clv_obligation_created": clv_obligation_created,
        "executable_cost": executable_cost,
        "net_edge": net_edge,
    }
    if extra:
        out.update(extra)
    return out


def sync_sports_market(
    market_data: dict,
    features: SportsEventFeatures,
    best_ask: float,
    fee_per_share: float,
    visible_depth: float,
    bankroll: float,
    risk_caps: RiskCaps,
    mode: Literal["live", "shadow", "paper"] = "shadow",
    real_orderbook_timestamp=None,
    real_received_timestamp=None,
    best_bid: Optional[float] = None,
    spread: Optional[float] = None,
    outcome_id: Optional[str] = None,
) -> dict[str, Any]:
    if mode != "live":
        # Hard guard to ensure no live orders can be placed from shadow/paper mode
        submit_live_orders = False
        assert submit_live_orders is False, "Cannot submit live orders in shadow mode"

    now = datetime.now(timezone.utc)
    age_ms = 0
    is_stale = False
    allow_received_shadow = (
        mode == "shadow"
        and bool(market_data.get("allow_received_timestamp_shadow"))
    )
    timestamp_source = market_data.get("timestamp_source")
    use_timestamp = real_orderbook_timestamp if real_orderbook_timestamp else (
        real_received_timestamp if allow_received_shadow else None
    )
    if use_timestamp:
        age_ms = (now - use_timestamp).total_seconds() * 1000.0
        if age_ms > 2000:
            is_stale = True
    if real_orderbook_timestamp is None and allow_received_shadow and real_received_timestamp is not None:
        timestamp_source = timestamp_source or "received_timestamp"

    token_id = (
        outcome_id
        or market_data.get("outcome_id")
        or (features.sport_specific or {}).get("outcome_token_id")
    )
    strategy = (features.sport_specific or {}).get("strategy") or "sports_quant_v1"

    snapshot_log = {
        "timestamp": now.isoformat(),
        "strategy": strategy,
        "platform": market_data.get("platform", "polymarket"),
        "market_id": features.market_id,
        "outcome_id": token_id,
        "received_timestamp": real_received_timestamp.isoformat() if real_received_timestamp else None,
        # Never label receipt time as exchange/orderbook timestamp
        "orderbook_timestamp": real_orderbook_timestamp.isoformat() if real_orderbook_timestamp else None,
        "exchange_timestamp": real_orderbook_timestamp.isoformat() if real_orderbook_timestamp else None,
        "missing_orderbook_timestamp": real_orderbook_timestamp is None,
        "timestamp_source": timestamp_source,
        "source": "api",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "visible_bid_depth": None,
        "visible_ask_depth": visible_depth,
        "age_ms": age_ms,
        "is_stale": is_stale,
        "missing_received_timestamp": real_received_timestamp is None,
    }
    with open("orderbook_snapshots.jsonl", "a") as f:
        f.write(json.dumps(snapshot_log) + "\n")

    # 1. Predict — moneyline/pitcher-outs may supply model_prob_override
    prediction = predict_sports_probability(features)
    override = None
    if isinstance(features.sport_specific, dict):
        override = features.sport_specific.get("model_prob_override")
    if override is None:
        override = market_data.get("model_prob_override")
    if override is not None:
        prediction.model_prob = float(override)
        if isinstance(features.sport_specific, dict):
            for k in (
                "model_version",
                "feature_version",
                "coefficient_source",
                "calibration_status",
                "model_type",
            ):
                if features.sport_specific.get(k) is not None and hasattr(prediction, k):
                    setattr(prediction, k, features.sport_specific[k])
            if features.sport_specific.get("model_type"):
                prediction.model_type = features.sport_specific["model_type"]
        # Recompute edge against market baseline after override
        market_p = prediction.market_prob
        if market_p is None:
            market_p = features.market_prob_baseline
        if market_p is not None:
            prediction.edge_before_execution = float(prediction.model_prob) - float(market_p)

    if prediction.rejection_reason:
        _log_decision(features, prediction, None, None, None, prediction.rejection_reason)
        return _sync_result(rejection_reason=prediction.rejection_reason)

    if getattr(prediction, "calibration_status", "uncalibrated_shadow") != "calibrated_out_of_sample" and mode == "live":
        raise ValueError("UNCALIBRATED_MODEL_LIVE_BLOCK")

    platform = (market_data.get("platform") or "").lower()
    kalshi_verified = bool(market_data.get("kalshi_moneyline_mapping_verified"))
    if platform == "kalshi" and not kalshi_verified:
        _log_decision(features, prediction, None, None, None, "KALSHI_SPORTS_MAPPING_NOT_IMPLEMENTED")
        return _sync_result(rejection_reason="KALSHI_SPORTS_MAPPING_NOT_IMPLEMENTED")

    if real_received_timestamp is None:
        _log_decision(features, prediction, None, None, None, "MISSING_ORDERBOOK_TIMESTAMP")
        return _sync_result(rejection_reason="MISSING_ORDERBOOK_TIMESTAMP")
    if real_orderbook_timestamp is None and not allow_received_shadow:
        _log_decision(features, prediction, None, None, None, "MISSING_ORDERBOOK_TIMESTAMP")
        return _sync_result(rejection_reason="MISSING_ORDERBOOK_TIMESTAMP")
    if real_orderbook_timestamp is not None and getattr(real_orderbook_timestamp, "tzinfo", None) is None:
        _log_decision(features, prediction, None, None, None, "NAIVE_ORDERBOOK_TIMESTAMP")
        return _sync_result(rejection_reason="NAIVE_ORDERBOOK_TIMESTAMP")
    if getattr(real_received_timestamp, "tzinfo", None) is None:
        _log_decision(features, prediction, None, None, None, "NAIVE_ORDERBOOK_TIMESTAMP")
        return _sync_result(rejection_reason="NAIVE_ORDERBOOK_TIMESTAMP")
    if visible_depth is None or float(visible_depth) <= 0:
        _log_decision(features, prediction, None, None, None, "INSUFFICIENT_DEPTH")
        return _sync_result(rejection_reason="INSUFFICIENT_DEPTH")
    if best_bid is None or spread is None:
        _log_decision(features, prediction, None, None, None, "MISSING_TOP_OF_BOOK")
        return _sync_result(rejection_reason="MISSING_TOP_OF_BOOK")
    if not token_id:
        _log_decision(features, prediction, None, None, None, "MISSING_OUTCOME_TOKEN_ID")
        return _sync_result(rejection_reason="MISSING_OUTCOME_TOKEN_ID")

    # Re-validate fee via model (reject unknown platforms; no fixed-fee invent)
    try:
        fee_check = estimate_fee_per_share(
            market_data.get("platform", "polymarket"),
            float(best_ask),
            1.0,
        )
    except ValueError as exc:
        _log_decision(features, prediction, None, None, None, str(exc))
        return _sync_result(rejection_reason=str(exc))
    fee_per_share = float(fee_per_share if fee_per_share is not None else fee_check)

    # 2. Build TradeCandidate (buy the selected outcome token)
    side = "YES"
    executable_cost = best_ask + fee_per_share + 0.005  # with slippage
    net_edge = float(prediction.model_prob) - float(executable_cost)

    if executable_cost >= 1.0:
        _log_decision(features, prediction, None, None, None, "EFFECTIVE_COST_NOT_TRADABLE")
        return _sync_result(
            rejection_reason="EFFECTIVE_COST_NOT_TRADABLE",
            executable_cost=executable_cost,
            net_edge=net_edge,
        )

    candidate = TradeCandidate(
        strategy=strategy,
        platform=market_data.get("platform", "polymarket"),
        market_id=features.market_id,
        outcome_id=str(token_id),
        event_id=features.event_id,
        side=side,
        model_prob=prediction.model_prob,
        market_prob=prediction.market_prob,
        executable_cost=executable_cost,
        best_bid=float(best_bid),
        best_ask=float(best_ask),
        spread=float(spread),
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
        return _sync_result(
            rejection_reason=sized_order.rejection_reason,
            executable_cost=executable_cost,
            net_edge=net_edge,
        )

    if sized_order.target_shares <= 0.0:
        _log_decision(features, prediction, sized_order, None, None, "ZERO_SIZED_ORDER")
        return _sync_result(
            rejection_reason="ZERO_SIZED_ORDER",
            executable_cost=executable_cost,
            net_edge=net_edge,
        )

    # 4. Paper Fill
    fill = simulate_paper_fill(
        order=sized_order,
        orderbook_timestamp=real_orderbook_timestamp,
        received_timestamp=real_received_timestamp,
        mode=mode,
        allow_received_timestamp_for_shadow=allow_received_shadow,
    )

    if fill.rejection_reason:
        _log_decision(features, prediction, sized_order, fill, None, fill.rejection_reason)
        return _sync_result(
            rejection_reason=fill.rejection_reason,
            executable_cost=executable_cost,
            net_edge=net_edge,
        )

    # 5. CLV Tracking — market fill vs effective cost stored separately
    # Stake for exposure = effective cost × shares (not market fill × shares).
    stake = float(fill.filled_shares) * float(fill.limit_price)
    close_lead = timedelta(minutes=5)
    event_start = features.start_time
    due_close = (
        (event_start - close_lead) if event_start is not None else None
    )
    clv_meta = {
        "event_id": features.event_id,
        "mode": mode,
        "platform": market_data.get("platform") or "unknown",
        "stake": stake,
        "shares": float(fill.filled_shares),
        "notional": stake,
        "entry_effective_cost": float(fill.limit_price),
        "entry_market_price": float(fill.simulated_fill_price),
        "event_start_utc": event_start.isoformat() if event_start is not None else None,
        "close_lead_minutes": 5,
        "exclude_from_clv_eval": bool(
            (features.sport_specific or {}).get("exclude_from_clv_eval")
        ),
        "slate_date": (features.sport_specific or {}).get("slate_date"),
        "game_pk": (features.sport_specific or {}).get("game_pk"),
    }
    clv_record = init_clv_record(
        trade_id=f"sports_{features.event_id}_{int(datetime.now(timezone.utc).timestamp())}",
        market_id=features.market_id,
        outcome_id=str(token_id),
        side=side,
        entry_price=fill.simulated_fill_price,
        entry_time=datetime.now(timezone.utc),
        platform=market_data.get("platform") or "unknown",
        due_close=due_close,
        entry_market_price=fill.simulated_fill_price,
        entry_effective_cost=fill.limit_price,
        metadata=clv_meta,
    )
    log_clv_record(clv_record, "sports_clv_tracking.jsonl")

    # Log successful decision
    _log_decision(features, prediction, sized_order, fill, clv_record, None)
    return _sync_result(
        would_trade=True,
        paper_filled=True,
        clv_obligation_created=True,
        executable_cost=executable_cost,
        net_edge=net_edge,
        extra={"clv_record_id": clv_record.trade_id},
    )


def _log_decision(
    features: SportsEventFeatures,
    prediction,
    order: SizedOrder,
    fill,
    clv_record,
    rejection_reason: str,
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
        "outcome_id": order.candidate.outcome_id if order else (features.sport_specific or {}).get("outcome_token_id"),
        "team_a": features.team_a,
        "team_b": features.team_b,
        "market_type": (features.sport_specific or {}).get("market_type", "moneyline"),
        "strategy": (features.sport_specific or {}).get("strategy"),
        "resolution_rule": (features.sport_specific or {}).get("resolution_rule", "standard"),
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
        "clv_obligation_created": clv_record is not None,
        "received_timestamp": order.candidate.received_timestamp.isoformat() if order and hasattr(order.candidate, "received_timestamp") and order.candidate.received_timestamp else None,
        "orderbook_timestamp": order.candidate.orderbook_timestamp.isoformat() if order and hasattr(order.candidate, "orderbook_timestamp") and order.candidate.orderbook_timestamp else None,
        "rejection_reason": rejection_reason,
        "would_trade": rejection_reason is None,

        # Settlement placeholders
        "settlement_result": None,
        "closing_price_snapshot": None,
        "final_score": None,
        "winning_side": None,
    }

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

    if log_entry["feature_snapshot"]:
        if "start_time" in log_entry["feature_snapshot"] and isinstance(log_entry["feature_snapshot"]["start_time"], datetime):
            log_entry["feature_snapshot"]["start_time"] = log_entry["feature_snapshot"]["start_time"].isoformat()
        if "snapshot_time" in log_entry["feature_snapshot"] and isinstance(log_entry["feature_snapshot"]["snapshot_time"], datetime):
            log_entry["feature_snapshot"]["snapshot_time"] = log_entry["feature_snapshot"]["snapshot_time"].isoformat()

    with open("sports_shadow_decisions.jsonl", "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    if fill and not fill.rejection_reason:
        fill_payload = dict(fill.__dict__)
        with open("sports_paper_fills.jsonl", "a") as f:
            f.write(json.dumps(fill_payload) + "\n")
