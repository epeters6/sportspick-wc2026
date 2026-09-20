"""Independent v3 collection and immutable forward forecasts for both US venues."""
from __future__ import annotations

import json
import math
import os
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import SOURCE, VERSION
from .contracts import NEIGHBORS, resolve, station_metadata
from .dataset import digest, finite, implementation_hash, partition_valid, utc
from .feeds import PublicFeeds
from .nowcast import weather_features
from .training import predict

DECISION_HOURS = (8, 11, 14, 17)


def normalized_quote(row):
    """Best-price fields are dollars; the normalized catalog uses yes_* cents.

    A one-cent quote must never be mistaken for a probability of one.
    """
    result = dict(row)
    for side in ("bid", "ask"):
        price = finite(row.get("best_" + side))
        if price is None:
            cents = finite(row.get("yes_" + side))
            price = cents / 100 if cents is not None else 0.0
        if not 0 <= price <= 1:
            raise ValueError("INVALID_QUOTE_UNITS")
        result["best_" + side] = price
    if result["best_bid"] > result["best_ask"] and result["best_ask"] > 0:
        raise ValueError("CROSSED_QUOTE")
    return result


def discover(feeds):
    """Public data only. No balance, signing, order or messaging methods."""
    os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
    pavlov_path = str(Path(__file__).resolve().parents[3] / "pavlov")
    if pavlov_path not in sys.path:
        sys.path.insert(0, pavlov_path)
    from pavlov.pipeline.kalshi_client import _WEATHER_SERIES_TICKERS, _parse_market
    markets, errors = [], []
    for series in _WEATHER_SERIES_TICKERS:
        try:
            page = feeds.fetch("https://external-api.kalshi.com/trade-api/v2/markets", {
                "series_ticker": series, "status": "open", "limit": 100,
            }, ttl=120)
            if page["data"].get("cursor"):
                # Avoid silently treating a truncated ladder as exhaustive.
                errors.append({"venue": "kalshi", "series": series, "reason": "DISCOVERY_PAGINATION_REQUIRED"})
                continue
            for raw in page["data"].get("markets", []):
                parsed = _parse_market({**raw, "received_timestamp": page["received_at"]}, series)
                parsed["_platform"] = "kalshi"
                markets.append(parsed)
        except Exception as exc:
            errors.append({"venue": "kalshi", "series": series, "reason": type(exc).__name__})
    try:
        from pavlov.polymarket.poly_client import get_weather_markets
        poly = get_weather_markets()
        markets.extend({**row, "_platform": "polymarket"} for row in poly)
        if not poly:
            errors.append({"venue": "polymarket", "reason": "NO_CURRENT_MARKETS"})
    except Exception as exc:
        errors.append({"venue": "polymarket", "reason": type(exc).__name__})
    return markets, errors


def refresh_quotes(markets, venue, feeds):
    if venue == "kalshi":
        from pavlov.pipeline.kalshi_client import _parse_market, get_weather_fee_metadata
        result = []
        for market in markets:
            response = feeds.fetch("https://external-api.kalshi.com/trade-api/v2/markets/" + market["ticker"], {}, ttl=0)
            raw = response["data"].get("market", {})
            if raw.get("ticker") != market["ticker"] or raw.get("status") not in ("active", "open"):
                raise ValueError("MARKET_NO_LONGER_OPEN")
            row = _parse_market({**raw, "received_timestamp": response["received_at"]}, market["series_ticker"])
            row.update(get_weather_fee_metadata(market["ticker"]))
            result.append(row)
        return result
    from pavlov.polymarket.poly_client import get_orderbook_as_parsed
    result = []
    for market in markets:
        book = get_orderbook_as_parsed(market["ticker"])
        if not book:
            raise ValueError("MISSING_POLYMARKET_BOOK")
        result.append({**market, **book})
    return result


def save_rows(db, rows):
    if not rows:
        return 0
    db.table("model_predictions").upsert(rows, on_conflict="id", ignore_duplicates=True).execute()
    persisted = db.table("model_predictions").select("id,metadata").in_("id", [r["id"] for r in rows]).execute().data or []
    found = {r["id"]: r["metadata"].get("decision_fingerprint") for r in persisted}
    if any(found.get(r["id"]) != r["metadata"]["decision_fingerprint"] for r in rows):
        raise ValueError("V3_IMMUTABLE_SNAPSHOT_CONFLICT")
    return len(rows)


def collect(output: Path, *, db=None, artifact=None, feeds=None, markets=None, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    feeds = feeds or PublicFeeds(output / "cache", clock=clock)
    run_id = str(uuid.uuid4())
    code_hash = implementation_hash()
    report = {"version": VERSION, "run_id": run_id, "started_at": started.isoformat(), "mode": "shadow",
              "live_ready": False, "paper_execution_enabled": False, "errors": [], "snapshots": 0,
              "groups": 0, "stations": [], "models": [], "rejected": {}, "venue_markets": {}}
    if db is not None:
        slot = "weather_learning_slot_v3:" + started.strftime("%Y%m%dT%H")
        db.table("app_settings").upsert({"key": slot, "value": {"run_id": run_id, "started_at": started.isoformat()}},
                                        on_conflict="key", ignore_duplicates=True).execute()
        claimed = db.table("app_settings").select("value").eq("key", slot).execute().data or []
        if len(claimed) != 1 or claimed[0]["value"].get("run_id") != run_id:
            report.update(status="skipped", reason="HOURLY_SLOT_ALREADY_ATTEMPTED")
            return report
    if markets is None:
        markets, report["errors"] = discover(feeds)
    output.mkdir(parents=True, exist_ok=True)
    (output / "market_capture.json").write_text(json.dumps(markets, default=str, allow_nan=False), encoding="utf-8")
    rejected, groups = Counter(), defaultdict(list)
    report["venue_markets"] = dict(Counter(r["_platform"] for r in markets))
    for market in markets:
        try:
            contract = resolve(market, market["_platform"])
            local = started.astimezone(ZoneInfo(station_metadata(contract.station)["timezone"]))
            lead = (datetime.fromisoformat(contract.target_date).date() - local.date()).days
            if local.hour not in DECISION_HOURS or not 0 <= lead <= 2 or utc(contract.window_end) <= started:
                rejected["OUTSIDE_RESEARCH_WINDOW"] += 1
                continue
            groups[(contract.platform, contract.station, contract.target_date, contract.metric, contract.authority)].append((contract, market))
        except (ValueError, KeyError, TypeError) as exc:
            rejected[str(exc)] += 1
    station_ids = {key[1] for key in groups}
    observing = station_ids | {neighbor for station in station_ids for neighbor in NEIGHBORS.get(station, [])}
    try:
        observations = feeds.observations(observing) if observing else None
    except Exception as exc:
        # Forecast-only evidence is retained with explicit missing-observation flags.
        observations = {"received_at": clock().isoformat(), "data": [], "content_hash": digest([])}
        report["errors"].append({"stage": "observations", "reason": type(exc).__name__})
    forecasts, station_details = {}, {}
    evidence = []
    for key, pairs in sorted(groups.items()):
        venue, station, target, metric, authority = key
        try:
            pairs.sort(key=lambda pair: -math.inf if pair[0].low_f is None else pair[0].low_f)
            if station not in forecasts:
                station_details[station] = feeds.station(station)
                forecasts[station] = feeds.forecast(station_details[station])
            # Weather is fetched first; quotes and final decision timestamp follow.
            fresh = refresh_quotes([m for _, m in pairs], venue, feeds)
            if len(fresh) != len(pairs):
                raise ValueError("INCOMPLETE_QUOTE_VECTOR")
            fresh = [normalized_quote(row) for row in fresh]
            for (old, _), new in zip(pairs, fresh):
                if resolve(new, venue).rules_hash != old.rules_hash:
                    raise ValueError("CONTRACT_RULES_CHANGED_DURING_CAPTURE")
            decision = clock()
            if any(not 0 <= (decision - utc(m["received_timestamp"])).total_seconds() <= 180 for m in fresh):
                raise ValueError("STALE_QUOTE_SNAPSHOT")
            from pavlov.pipeline.market_probability import generate_market_implied_vector
            from pavlov.pipeline.execution_cost import generate_executable_cost_vector
            market_probs = generate_market_implied_vector(fresh)
            costs, depths = [], []
            for raw in fresh:
                try:
                    q, depth = generate_executable_cost_vector([raw], venue)
                    costs.append(q[0]); depths.append(depth[0])
                except ValueError:
                    costs.append(None); depths.append(0)
            rows = []
            for i, ((contract, _), raw) in enumerate(zip(pairs, fresh)):
                weather = weather_features(contract, forecasts[station], observations, decision)
                probability = weather["nowcast_probability"]
                row = {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{run_id}:{venue}:{contract.market_id}")),
                    "source": SOURCE, "domain": "weather", "event_key": f"weather:{venue}:{station}:{target}:{metric}",
                    "outcome": contract.market_id, "prob": probability,
                    "market_price": finite(raw.get("best_ask"), finite(raw.get("yes_ask"), 0) / 100),
                    "edge": None, "created_at": decision.isoformat(), "resolved_at": None, "is_correct": None,
                    "metadata": {
                        "experiment_id": SOURCE, "run_id": run_id, "decision_at": decision.isoformat(),
                        "station": station, "station_verified": True, "platform": venue,
                        "venue_product": "polymarket_us" if venue == "polymarket" else "kalshi",
                        "metric": metric, "target_date": target, "settlement_source": authority,
                        "observation_timezone": contract.observation_timezone,
                        "lead_days": (datetime.fromisoformat(target).date() - decision.astimezone(ZoneInfo(contract.observation_timezone)).date()).days,
                        "bucket_low_f": contract.low_f, "bucket_high_f": contract.high_f,
                        "raw_model_prob": probability, "market_vector_prob": market_probs[i],
                        "executable_cost": costs[i], "ask_depth": depths[i], "fee_estimate": raw.get("fee_estimate"),
                        "quote_received_at": utc(raw["received_timestamp"]).isoformat(),
                        "weather_features": weather, "station_coordinates": station_details[station],
                        "contract": contract.record(), "snapshot_kind": "pre_execution_forecast",
                        "weather_forecast_received_at": forecasts[station]["received_at"],
                        "weather_forecast_hash": forecasts[station]["content_hash"],
                        "observations_received_at": observations["received_at"], "observations_hash": observations["content_hash"],
                        "execution_enabled": False, "model_version": VERSION, "implementation_hash": code_hash,
                        "eligible_before_observation_gate": False,
                    },
                }
                rows.append(row)
            if not partition_valid(rows):
                raise ValueError("INCOMPLETE_CONTRACT_PARTITION")
            model_predictions = predict(rows, artifact)
            total = sum(r["prob"] for r in rows)
            for i, row in enumerate(rows):
                row["prob"] /= total
                row["metadata"]["challenger_probabilities"] = {name: p[i] for name, p in model_predictions.items()}
                row["metadata"]["model_id"] = artifact["model_id"] if artifact else None
                row["metadata"]["model_trained_at"] = artifact["trained_at"] if artifact else None
                row["metadata"]["selected_model"] = artifact["selected"] if artifact else "untrained_nowcast_prior"
                if artifact:
                    row["prob"] = model_predictions[artifact["selected"]][i]
                row["metadata"]["decision_fingerprint"] = digest(row)
            if db is not None:
                save_rows(db, rows)
            evidence.extend(rows)
            report["snapshots"] += len(rows)
            report["groups"] += 1
            report["stations"].append(station)
            report["models"].append(weather["model_count"])
        except Exception as exc:
            report["errors"].append({"stage": "station_group", "venue": venue, "station": station,
                                     "target_date": target, "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    report["rejected"] = dict(rejected)
    report["stations"] = sorted(set(report["stations"]))
    report["completed_at"] = clock().isoformat()
    report["status"] = "degraded" if report["errors"] else "healthy"
    output.mkdir(parents=True, exist_ok=True)
    (output / "snapshots.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in evidence), encoding="utf-8")
    (output / "collection.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report
