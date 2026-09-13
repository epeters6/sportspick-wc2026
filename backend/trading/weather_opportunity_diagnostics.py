"""Explain a captured discovery batch without fetching, trading, or changing it.

This observer is deliberately outside the frozen strategy implementation. Call
it after the research suite returns, using that suite's shared_inputs reference.
Eligibility is evaluated at the supplied diagnostic time, not asserted to be the
time at which each strategy arm processed a market.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import math
import re
from zoneinfo import ZoneInfo

from backend.trading.weather_experiment import event_scope_reason
from pavlov.pipeline.settlement_resolver import normalize_market
from pavlov.pipeline.station_mapper import get_tz_for_city


_VENUES = {"kalshi", "polymarket"}
_MARKET_ID = re.compile(
    r"(?:KX[A-Z0-9]+-\d{2}[A-Z]{3}\d{2}-[A-Z0-9.]+|"
    r"tc-temp-[a-z0-9-]+-\d{4}-\d{2}-\d{2}-[a-z0-9.-]+)\Z"
)
_STATION_REASONS = {
    "UNSUPPORTED_SETTLEMENT_STATION", "UNSUPPORTED_WEATHER_VENUE",
    "MISSING_EXPLICIT_SETTLEMENT_STATION", "CONFLICTING_SETTLEMENT_STATIONS",
    "CONFLICTING_SETTLEMENT_CITY", "CONFLICTING_SERIES_SETTLEMENT_STATION",
    "UNVERIFIED_SETTLEMENT_SOURCE", "UNVERIFIED_OBSERVATION_DAY",
    "CONFLICTING_OBSERVATION_DAY",
}


def _market_id(value):
    # Return an exact public weather ID or omit it; never echo arbitrary input.
    return value if isinstance(value, str) and len(value) <= 180 and _MARKET_ID.fullmatch(value) else None


def _timestamp(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _bound(value):
    if math.isnan(value):
        return None
    if math.isinf(value):
        return "-inf" if value < 0 else "+inf"
    return value


def _venue_summary():
    return {key: 0 for key in (
        "input_rows", "normalized_rows", "station_rejected_rows", "skipped_rows",
        "error_rows", "groups", "complete_partitions", "invalid_partitions",
    )}


def _config_valid(config):
    if not isinstance(config, dict):
        return False
    identity = config.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"weather_[A-Za-z0-9_.-]{1,150}", identity):
        return False
    for key, allowed in (("venues", _VENUES), ("metrics", {"high", "low"})):
        values = config.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or v not in allowed for v in values):
            return False
    stations = config.get("stations")
    hours = config.get("decision_hours_local")
    lead = config.get("max_lead_days")
    return (
        isinstance(stations, list) and bool(stations)
        and all(isinstance(s, str) and re.fullmatch(r"K[A-Z0-9]{3}", s) for s in stations)
        and isinstance(hours, list) and bool(hours)
        and all(type(hour) is int and 0 <= hour <= 23 for hour in hours)
        and type(lead) is int and lead >= 0
    )


def _partition(events):
    # Match the frozen sync's grouping and ordering, including duplicate rows.
    events = sorted(events, key=lambda e: (e.bucket_low_f, e.bucket_high_f, e.market_id))
    issues = []
    if events[0].bucket_low_f != float("-inf"):
        issues.append({"reason": "MISSING_LOWER_TAIL", "first_low_f": _bound(events[0].bucket_low_f)})
    if events[-1].bucket_high_f != float("inf"):
        issues.append({"reason": "MISSING_UPPER_TAIL", "last_high_f": _bound(events[-1].bucket_high_f)})
    for index, event in enumerate(events):
        lo, hi = event.bucket_low_f, event.bucket_high_f
        if math.isnan(lo) or math.isnan(hi):
            issues.append({"reason": "INVALID_NUMERIC_BOUND", "bucket_index": index})
        elif lo >= hi:
            issues.append({"reason": "NON_INCREASING_BUCKET", "bucket_index": index})
    for index, (left, right) in enumerate(zip(events, events[1:])):
        if left.bucket_high_f != right.bucket_low_f:
            hi, lo = left.bucket_high_f, right.bucket_low_f
            reason = "GAP" if hi < lo else "OVERLAP" if hi > lo else "INVALID_NUMERIC_BOUND"
            issues.append({"reason": reason, "left_bucket_index": index,
                           "right_bucket_index": index + 1,
                           "left_high_f": _bound(hi), "right_low_f": _bound(lo)})
    identities = Counter(e.market_id for e in events)
    return events, {
        "complete": not issues, "bucket_count": len(events), "issues": issues,
        "duplicate_market_ids": sorted(identity for identity, count in identities.items()
                                       if count > 1 and _market_id(identity)),
        "buckets": [{"market_id": _market_id(e.market_id),
                     "market_id_omitted": _market_id(e.market_id) is None,
                     "low_f": _bound(e.bucket_low_f), "high_f": _bound(e.bucket_high_f)} for e in events],
    }


def build_discovery_diagnostics(shared_inputs: dict, configs: list[dict], *, now: datetime) -> dict:
    """Return JSON-safe diagnostics of the exact captured input, with no I/O.

    Missing capture is explicitly ``not_captured``: outside-window and duplicate
    slot runs legitimately have none. It must not be read as zero opportunities.
    Raw descriptions, metadata, quotes, credentials and exception text are never
    included. Normalization uses a deep copy so even a mutating parser cannot
    change a captured strategy input.
    """
    stamp = _timestamp(now)
    if not isinstance(now, datetime) or stamp is None:
        raise ValueError("DIAGNOSTIC_TIME_MUST_BE_TIMEZONE_AWARE")
    now = now.astimezone(timezone.utc)
    shared = shared_inputs if isinstance(shared_inputs, dict) else {}
    capture_id = shared.get("capture_id")
    if not isinstance(capture_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", capture_id,
    ):
        capture_id = None
    result = {
        "schema_version": 1, "diagnostic_only": True, "live_ready": False,
        "status": "not_captured", "reason": "MARKET_CAPTURE_NOT_AVAILABLE",
        "eligibility_as_of": stamp,
        "eligibility_basis": "supplied_diagnostic_time_not_individual_execution_time",
        "capture_id": capture_id, "captured_at": _timestamp(shared.get("captured_at")),
        "capture_warnings": [], "discovery": None,
        "normalization": None, "summary": None, "venues": {},
        "groups": [], "experiments": {}, "config_errors": [],
    }
    capture = shared.get("market_capture")
    if capture is None:
        return result
    if not isinstance(capture, dict) or not isinstance(capture.get("markets"), list):
        result.update(status="invalid_capture", reason="MARKET_CAPTURE_ROWS_INVALID")
        return result
    rows = capture["markets"]
    result.update(status="ok", reason=None)
    for field in ("capture_id", "captured_at"):
        if result[field] is None:
            result["capture_warnings"].append(f"{field.upper()}_UNAVAILABLE")
    stats = capture.get("stats")
    errors = stats.get("venue_errors") if isinstance(stats, dict) else None
    result["discovery"] = {
        "health_known": isinstance(errors, dict),
        "failed_venues": sorted(venue for venue in _VENUES if isinstance(errors, dict) and errors.get(venue)),
        "unclassified_failure_present": bool(isinstance(errors, dict) and any(
            key not in _VENUES and value for key, value in errors.items())),
    }
    normal = {"input_rows": len(rows), "normalized_rows": 0, "station_rejected_rows": 0,
              "skipped_rows": 0, "error_rows": 0, "reasons": {}, "issues": []}
    result["normalization"] = normal
    result["summary"] = {"groups": 0, "complete_partitions": 0, "invalid_partitions": 0}
    groups = defaultdict(list)
    for index, raw in enumerate(rows):
        venue = raw.get("_platform") if isinstance(raw, dict) else None
        venue = venue if isinstance(venue, str) and venue in _VENUES else "unknown"
        venue_summary = result["venues"].setdefault(venue, _venue_summary())
        venue_summary["input_rows"] += 1
        identity = _market_id(raw.get("ticker")) if isinstance(raw, dict) else None
        reason = category = None
        if not isinstance(raw, dict):
            reason, category = "MARKET_ROW_NOT_OBJECT", "error_rows"
        elif venue == "unknown":
            reason, category = "UNSUPPORTED_WEATHER_VENUE", "station_rejected_rows"
        else:
            try:
                event = normalize_market(deepcopy(raw), venue, require_verified_station=True)
                if event is None:
                    reason, category = "BUCKET_OR_DATE_NORMALIZATION_SKIPPED", "skipped_rows"
                else:
                    # Identical tuple to sync_weather: source and reporting window
                    # keep unrelated settlement definitions out of one partition.
                    key = (event.settlement_station, event.settlement_source, event.date,
                           event.observation_window, venue, event.metric)
                    groups[key].append(event)
            except ValueError as exc:
                code = str(exc).split(":", 1)[0]
                if code in _STATION_REASONS:
                    reason, category = code, "station_rejected_rows"
                else:
                    reason, category = "NORMALIZATION_ERROR", "error_rows"
            except Exception:
                reason, category = "NORMALIZATION_ERROR", "error_rows"
        if category:
            normal[category] += 1
            venue_summary[category] += 1
            normal["reasons"][reason] = normal["reasons"].get(reason, 0) + 1
            normal["issues"].append({"row_index": index, "venue": venue,
                                     "market_id": identity, "reason": reason})
        else:
            normal["normalized_rows"] += 1
            venue_summary["normalized_rows"] += 1

    valid_configs = []
    for index, config in enumerate(configs if isinstance(configs, list) else []):
        if not _config_valid(config) or config["id"] in result["experiments"]:
            result["config_errors"].append({"index": index, "reason": "INVALID_OR_DUPLICATE_DIAGNOSTIC_CONFIG"})
            continue
        valid_configs.append(config)
        result["experiments"][config["id"]] = {
            "eligible_complete_groups": 0, "eligible_invalid_groups": 0,
            "outside_scope_complete_groups": 0, "outside_scope_invalid_groups": 0,
            "eligibility_unknown_groups": 0, "outside_scope_reasons": {},
        }
    if not valid_configs and not result["config_errors"]:
        result["config_errors"].append({"reason": "DIAGNOSTIC_CONFIGS_NOT_AVAILABLE"})

    for key, members in sorted(groups.items(), key=lambda pair: repr(pair[0])):
        station, source, target, window, venue, metric = key
        events, partition = _partition(members)
        group = {
            "station": station, "venue": venue, "target_date": target.isoformat(),
            "metric": metric, "settlement_source": source,
            "reporting_timezone": events[0].observation_timezone,
            "reporting_window_utc": [_timestamp(value) for value in window] if window else None,
            "partition": partition, "eligibility": {},
        }
        field = "complete_partitions" if partition["complete"] else "invalid_partitions"
        result["summary"]["groups"] += 1
        result["summary"][field] += 1
        result["venues"][venue]["groups"] += 1
        result["venues"][venue][field] += 1
        for config in valid_configs:
            summary = result["experiments"][config["id"]]
            try:
                local = now.astimezone(ZoneInfo(get_tz_for_city(events[0].city)))
                lead = (target - local.date()).days
                reason = event_scope_reason(config, venue=venue, station=station,
                    metric=metric, lead_days=lead, local_hour=local.hour)
                eligible = reason is None
                group["eligibility"][config["id"]] = {
                    "eligible_as_of": eligible, "reason": reason,
                    "station_local_hour": local.hour, "lead_days": lead,
                }
                count = ("eligible_" if eligible else "outside_scope_") + ("complete_groups" if partition["complete"] else "invalid_groups")
                summary[count] += 1
                if reason:
                    summary["outside_scope_reasons"][reason] = summary["outside_scope_reasons"].get(reason, 0) + 1
            except Exception:
                summary["eligibility_unknown_groups"] += 1
                group["eligibility"][config["id"]] = {"eligible_as_of": None, "reason": "ELIGIBILITY_DIAGNOSTIC_ERROR"}
        result["groups"].append(group)
    if result["discovery"]["failed_venues"] or result["discovery"]["unclassified_failure_present"]:
        result.update(status="incomplete_capture", reason="VENUE_DISCOVERY_FAILED")
    elif normal["error_rows"] or result["config_errors"]:
        result.update(status="diagnostic_error", reason="DIAGNOSTIC_INPUT_OR_NORMALIZATION_ERROR")
    elif any(summary["eligibility_unknown_groups"] for summary in result["experiments"].values()):
        result.update(status="diagnostic_error", reason="ELIGIBILITY_DIAGNOSTIC_ERROR")
    elif result["summary"]["invalid_partitions"]:
        result.update(status="issues_found", reason="INVALID_BUCKET_PARTITIONS_IN_CAPTURE")
    elif not rows:
        result.update(status="empty_capture", reason="CAPTURE_CONTAINS_NO_MARKETS")
    return result
