"""Read-only evidence report for a frozen weather paper experiment.

Snapshot diagnostics are event-balanced; bankroll results use verified paper
fills only. Confidence intervals resample whole target dates, retaining the
dependence between stations and trades on a date. No report promotes live mode.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import math
import random
import re

from backend.trading.settlement_integrity import verify_weather_autobet
from backend.trading.weather_experiment import fetch_experiment_bets, metadata

VENUES = ("kalshi", "polymarket")
PAGE_SIZE = 500
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20260906


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _event(meta):
    station, target, metric = meta.get("station"), meta.get("target_date"), meta.get("metric")
    try:
        date.fromisoformat(str(target))
    except ValueError:
        return None
    return (str(station), str(target), metric) if station and metric in {"high", "low"} else None


def _pages(db, table, filters, order="id"):
    rows, cursor = [], None
    while True:
        query = db.table(table).select("*")
        for key, value in filters:
            query = query.eq(key, value)
        query = query.order(order).limit(PAGE_SIZE)
        if cursor is not None:
            query = query.gt(order, cursor)
        batch = query.execute().data or []
        if not batch:
            return rows
        next_cursor = str(batch[-1][order])
        if cursor is not None and next_cursor <= cursor:
            raise ValueError(f"NON_ADVANCING_PAGINATION:{table}")
        rows.extend(batch)
        cursor = next_cursor


def _deduplicate(rows, key, quarantine):
    seen, conflicts = {}, set()
    for row in rows:
        identity = row.get(key)
        if not identity:
            quarantine["MISSING_RECORD_ID"] += 1
        elif identity in seen and row != seen[identity]:
            conflicts.add(identity)
        else:
            seen[identity] = row
    for identity in conflicts:
        seen.pop(identity, None)
        quarantine["CONFLICTING_DUPLICATE_ID"] += 1
    return list(seen.values())


def _forecast_report(rows, manifest):
    config = manifest.get("config", manifest)
    quarantine, groups = Counter(), defaultdict(list)
    scoped = [row for row in rows if row.get("source") == config["prediction_source"]
              and metadata(row.get("metadata")).get("experiment_id") == manifest["id"]]
    for row in _deduplicate(scoped, "id", quarantine):
        meta = metadata(row.get("metadata"))
        if not row.get("event_key") or not meta.get("run_id"):
            quarantine["MISSING_SNAPSHOT_IDENTITY"] += 1
            continue
        groups[(row["event_key"], meta["run_id"])].append(row)
    scored = defaultdict(list)
    valid_row_ids, pending, valid_snapshots, paired_rows = set(), 0, 0, 0
    for (event_key, _), vector in groups.items():
        first = metadata(vector[0].get("metadata"))
        event, venue = _event(first), first.get("platform")
        reason = None
        probabilities, prices, bounds, outcomes = [], [], [], set()
        for row in vector:
            meta = metadata(row.get("metadata"))
            p, market = _number(row.get("prob")), _number(row.get("market_price"))
            created = _timestamp(row.get("created_at"))
            if (not event or venue not in VENUES or _event(meta) != event or meta.get("platform") != venue
                    or event_key != f"weather:{venue}:{event[0]}:{event[1]}:{event[2]}"
                    or meta.get("snapshot_kind") != "pre_execution_forecast" or not created
                    or (manifest.get("config_hash") and meta.get("config_hash") != manifest["config_hash"])):
                reason = "INVALID_SNAPSHOT_PROVENANCE"
                break
            if p is None or market is None or not 0 <= p <= 1 or not 0 <= market <= 1:
                reason = "INVALID_SNAPSHOT_PROBABILITY"
                break
            outcome = row.get("outcome")
            if not outcome or outcome in outcomes or "bucket_low_f" not in meta or "bucket_high_f" not in meta:
                reason = "INVALID_BUCKET_IDENTITY"
                break
            lo = -math.inf if meta["bucket_low_f"] is None else _number(meta["bucket_low_f"])
            hi = math.inf if meta["bucket_high_f"] is None else _number(meta["bucket_high_f"])
            if lo is None or hi is None or lo >= hi:
                reason = "INVALID_BUCKET_BOUNDS"
                break
            probabilities.append(p)
            prices.append(market)
            bounds.append((lo, hi))
            outcomes.add(outcome)
        if not reason:
            bounds.sort()
            if (bounds[0][0] != -math.inf or bounds[-1][1] != math.inf
                    or any(abs(left[1] - right[0]) > 1e-6 for left, right in zip(bounds, bounds[1:]))
                    or abs(sum(probabilities) - 1) > 1e-5 or abs(sum(prices) - 1) > 1e-5):
                reason = "INCOMPLETE_OR_UNNORMALIZED_VECTOR"
        if reason:
            quarantine[reason] += 1
            continue
        if any(row.get("is_correct") is None for row in vector):
            pending += 1
            continue
        for row in vector:
            meta = metadata(row.get("metadata"))
            resolved, created = _timestamp(row.get("resolved_at")), _timestamp(row.get("created_at"))
            if (type(row.get("is_correct")) is not bool or meta.get("label_source") != "venue_official"
                    or meta.get("official_venue") != venue or meta.get("official_market_id") != row["outcome"]
                    or meta.get("official_result") not in {"yes", "no"}
                    or row["is_correct"] != (meta["official_result"] == "yes")
                    or not resolved or resolved <= created):
                reason = "INVALID_OFFICIAL_LABEL"
                break
        if not reason and sum(row["is_correct"] for row in vector) != 1:
            reason = "OFFICIAL_VECTOR_WINNER_COUNT"
        if reason:
            quarantine[reason] += 1
            continue
        n = len(vector)
        scored[(venue, event)].append((
            sum((p - int(row["is_correct"])) ** 2 for p, row in zip(probabilities, vector)) / n,
            sum((p - int(row["is_correct"])) ** 2 for p, row in zip(prices, vector)) / n,
        ))
        valid_row_ids.update(row["id"] for row in vector)
        valid_snapshots += 1
        paired_rows += n
    venues = {}
    for venue in VENUES:
        events = [scores for (platform, _), scores in scored.items() if platform == venue]
        model = [sum(score[0] for score in scores) / len(scores) for scores in events]
        market = [sum(score[1] for score in scores) / len(scores) for scores in events]
        venues[venue] = {
            "independent_events": len(events),
            "model_brier": sum(model) / len(model) if model else None,
            "market_brier": sum(market) / len(market) if market else None,
            "model_minus_market_brier": (sum(model) - sum(market)) / len(model) if model else None,
        }
    return {
        "fetched_rows": len(rows), "scoped_rows": len(scoped), "snapshots": len(groups),
        "official_valid_snapshots": valid_snapshots, "pending_snapshots": pending,
        "paired_bucket_rows": paired_rows,
        "independent_events": len({event for _, event in scored}),
        "quarantined": sum(quarantine.values()), "quarantine_reasons": dict(quarantine),
        "brier_weighting": "mean bucket error per snapshot, then mean snapshots per station/date/metric, then mean events per venue",
        "venues": venues,
    }, valid_row_ids


def _percentile(values, quantile):
    point = (len(values) - 1) * quantile
    lower = int(point)
    upper = min(lower + 1, len(values) - 1)
    weight = point - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _bootstrap_roi(trades):
    if not trades or any(_number(trade["stake"]) is None or trade["stake"] <= 0
                         or _number(trade["pnl"]) is None for trade in trades):
        return None
    # A common scale leaves ROI unchanged and avoids overflow in day totals.
    scale = max(max(trade["stake"], abs(trade["pnl"])) for trade in trades)
    days = defaultdict(lambda: [0.0, 0.0])
    for trade in trades:
        days[trade["event"][1]][0] += trade["stake"] / scale
        days[trade["event"][1]][1] += trade["pnl"] / scale
    clusters = [days[day] for day in sorted(days)]
    if len(clusters) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    samples = []
    for _ in range(BOOTSTRAP_REPLICATES):
        draw = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        denominator = sum(day[0] for day in draw)
        roi = _number(sum(day[1] for day in draw) / denominator) if denominator > 0 else None
        if roi is None:
            return None
        samples.append(roi)
    samples.sort()
    interval = [_percentile(samples, 0.025), _percentile(samples, 0.975)]
    return interval if all(math.isfinite(value) for value in interval) else None


def _finite_sum(values):
    try:
        return _number(math.fsum(values))
    except OverflowError:
        return None


def _venue_report(venue, bets, clv, manifest, valid_snapshots, reads_complete, forecast_valid):
    config = manifest.get("config", manifest)
    quarantine, trades, open_count = Counter(), [], 0
    scoped = [bet for bet in bets if bet.get("venue") == venue and bet.get("mode") == "paper"
              and metadata(bet.get("metadata")).get("experiment_id") == manifest["id"]]
    for bet in _deduplicate(scoped, "id", quarantine):
        meta = metadata(bet.get("metadata"))
        if bet.get("status") == "open":
            open_count += 1
            continue
        event = _event(meta)
        check = verify_weather_autobet(bet, allow_legacy_paper=False)
        stake, shares, q_exec = _number(bet.get("stake")), _number(bet.get("shares")), _number(meta.get("q_exec"))
        created, resolved = _timestamp(bet.get("created_at")), _timestamp(bet.get("resolved_at"))
        settlement = metadata(meta.get("settlement"))
        if (not check.valid or not event or not created or not resolved or resolved <= created
                or settlement.get("source") != f"{venue}_official"
                or (manifest.get("config_hash") and meta.get("config_hash") != manifest["config_hash"])):
            quarantine[check.reason or "INVALID_TRADE_PROVENANCE"] += 1
            continue
        effective = _number(stake / shares)
        if (q_exec is None or not 0 < q_exec < 1 or effective is None or not 0 < effective < 1
                or q_exec < float(bet["market_price"])
                or abs(stake - shares * q_exec) > 0.0100001):
            quarantine["INVALID_EFFECTIVE_COST_EVIDENCE"] += 1
            continue
        trades.append({"bet": bet, "meta": meta, "event": event, "stake": stake,
                       "shares": shares, "pnl": float(check.expected_pnl), "effective": effective})

    clv_scoped = [row for row in clv if row.get("platform") == venue
                  and metadata(row.get("metadata")).get("experiment_id") == manifest["id"]]
    clv_by_candidate = {row["candidate_id"]: row for row in _deduplicate(clv_scoped, "candidate_id", quarantine)}
    event_trades = defaultdict(list)
    observed_count = 0
    for trade in trades:
        event_trades[trade["event"]].append(trade)
        bet = trade["bet"]
        signal_id = trade["meta"].get("candidate_id")
        fill_id = f"weather-fill:{bet['id']}"
        if not isinstance(signal_id, str) or not signal_id:
            quarantine["INVALID_TRADED_CLOSING_CLV"] += 1
            continue
        # The execution pipeline keys obligations by durable fill identity.
        # Retain the original signal-key layout for existing paper evidence,
        # but never silently choose between two obligations for the same fill.
        linked = [clv_by_candidate[key] for key in (fill_id, signal_id)
                  if key in clv_by_candidate]
        if len(linked) > 1:
            quarantine["AMBIGUOUS_TRADED_CLOSING_CLV"] += 1
            continue
        row = linked[0] if linked else None
        if not row or row.get("status_close") != "observed":
            continue
        meta = metadata(row.get("metadata"))
        if ((row["candidate_id"] == fill_id and meta.get("signal_candidate_id") != signal_id)
                or (meta.get("signal_candidate_id") is not None and meta["signal_candidate_id"] != signal_id)):
            quarantine["INVALID_TRADED_CLOSING_CLV"] += 1
            continue
        close, effective = _number(row.get("obs_close_price")), _number(row.get("entry_effective_cost"))
        observed, due, entry = _timestamp(row.get("obs_close_ts")), _timestamp(row.get("due_close")), _timestamp(row.get("entry_ts"))
        if (row.get("market_id") != bet.get("market_id") or str(row.get("side")).lower() != str(bet.get("outcome_name")).lower()
                or meta.get("autobet_id") != bet["id"] or (manifest.get("config_hash") and meta.get("config_hash") != manifest["config_hash"])
                or close is None or not 0 <= close <= 1 or effective is None or abs(effective - trade["effective"]) > 0.01
                or not observed or not due or not entry or not entry < observed < _timestamp(bet["resolved_at"])
                or abs((observed - due).total_seconds()) > 600):
            quarantine["INVALID_TRADED_CLOSING_CLV"] += 1
            continue
        trade["net_clv"] = close - trade["effective"]
        observed_count += 1
    complete_events = [rows for rows in event_trades.values() if all("net_clv" in row for row in rows)]
    event_clv = []
    for rows in complete_events:
        scale = max(row["shares"] for row in rows)
        event_clv.append(sum(row["net_clv"] * (row["shares"] / scale) for row in rows)
                         / sum(row["shares"] / scale for row in rows))
    coverage = len(complete_events) / len(event_trades) if event_trades else 0.0
    net_clv = sum(event_clv) / len(event_clv) if event_clv else None
    stake, pnl = _finite_sum(row["stake"] for row in trades), _finite_sum(row["pnl"] for row in trades)
    net_roi = _number(pnl / stake) if stake and pnl is not None else None
    if stake is None or pnl is None or (stake and net_roi is None):
        quarantine["NON_FINITE_ACCOUNT_AGGREGATE"] += 1
    interval = _bootstrap_roi(trades)
    dates = len({row["event"][1] for row in trades})
    criteria = {
        "reads_complete": reads_complete,
        "no_quarantined_evidence": not quarantine and forecast_valid,
        "minimum_independent_traded_events": len(event_trades) >= max(250, int(config.get("min_forward_events", 250))),
        "minimum_distinct_traded_dates": dates >= max(45, int(config.get("min_forward_days", 45))),
        "positive_lower_95pct_day_cluster_net_roi": interval is not None and interval[0] > 0,
        "positive_net_closing_clv": net_clv is not None and net_clv > 0,
        "closing_clv_event_coverage": coverage >= 0.8 and len(complete_events) >= 30,
        "immutable_traded_decisions_verified": bool(trades) and all(row["meta"].get("decision_snapshot_id") in valid_snapshots for row in trades),
    }
    return {
        "paper_only": True, "live_ready": False, "research_evidence_ready": all(criteria.values()),
        "criteria": criteria, "blocked_reasons": [key for key, passed in criteria.items() if not passed],
        "scoped_bets": len(scoped), "open_bets": open_count, "verified_settled_bets": len(trades),
        "independent_traded_events": len(event_trades), "distinct_traded_dates": dates,
        "verified_stake": stake, "verified_realized_pnl": pnl, "net_roi": net_roi,
        "net_roi_95pct_day_cluster_interval": interval,
        "closing_clv_observed_bets": observed_count, "closing_clv_complete_events": len(complete_events),
        "closing_clv_event_coverage": coverage, "mean_net_closing_clv": net_clv,
        "quarantined": sum(quarantine.values()), "quarantine_reasons": dict(quarantine),
    }


def build_forward_report(db, manifest, bets=None) -> dict:
    """Select experiment evidence and evaluate research gates; never enable live trading."""
    config = manifest.get("config", manifest)
    source = config.get("prediction_source")
    calibration_mode = config.get("execution_calibration_mode", "required")
    if (not manifest.get("id") or not isinstance(source, str)
            or re.fullmatch(r"weather_forward_[A-Za-z0-9_]+", source) is None
            or config.get("mode", "paper") != "paper"
            or calibration_mode not in ("required", "observe_only")):
        raise ValueError("INVALID_WEATHER_FORWARD_MANIFEST")
    errors = []
    try:
        predictions = _pages(db, "model_predictions", [("source", config["prediction_source"]), ("metadata->>experiment_id", manifest["id"])])
    except Exception as exc:
        predictions = []
        errors.append(f"PREDICTION_READ_FAILED:{type(exc).__name__}")
    if bets is None:
        try:
            bets = fetch_experiment_bets(db, manifest["id"])
        except Exception as exc:
            bets = []
            errors.append(f"BET_READ_FAILED:{type(exc).__name__}")
    try:
        clv = _pages(db, "clv_obligations", [("metadata->>experiment_id", manifest["id"])], order="candidate_id")
    except Exception as exc:
        clv = []
        errors.append(f"CLV_READ_FAILED:{type(exc).__name__}")
    forecast, valid_snapshots = _forecast_report(predictions, manifest)
    venues = {venue: _venue_report(venue, bets, clv, manifest, valid_snapshots, not errors, forecast["quarantined"] == 0) for venue in VENUES}
    return {
        "experiment_id": manifest["id"], "prediction_source": config["prediction_source"],
        "execution_calibration_mode": calibration_mode,
        "historical_execution_filter_required": calibration_mode == "required",
        "research_label": config.get("research_label", "Frozen weather paper research"),
        "exploratory": config.get("exploratory") is True,
        "generated_at": datetime.now(timezone.utc).isoformat(), "live_ready": False,
        "research_evidence_ready": all(row["research_evidence_ready"] for row in venues.values()),
        "research_evidence_ready_venues": [venue for venue, row in venues.items() if row["research_evidence_ready"]],
        "forecast": forecast, "venues": venues, "read_errors": errors,
        "bootstrap": {"method": "target-date cluster percentile", "replicates": BOOTSTRAP_REPLICATES, "seed": BOOTSTRAP_SEED, "confidence": 0.95},
        "limitations": ["Paper fill assumptions are not verified live execution.", "Research evidence never grants live trading permission.", "Repeated report inspection and strategy selection are not adjusted in the exploratory confidence interval."]
        + (["This exploratory arm records historical calibration but does not enforce its execution filter."]
           if calibration_mode == "observe_only" else []),
    }
