"""Exact-run receipts for prospective research. Initialization is not publication."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from backend.ml.weather_learning.dataset import digest
from backend.weather_historical_calibration import ARCHIVE, standard_day

# gfs_global temperature comes from the primary gfs013 reader; gfs025 is a
# supplemental reader for other variables and can advertise a different run.
MODEL_DOMAINS = {"gfs_global": "ncep_gfs013", "gfs_hrrr": "ncep_hrrr_conus", "ecmwf_ifs025": "ecmwf_ifs025"}


def timestamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("RUN_TIME_REQUIRES_TIMEZONE")
    return result.astimezone(timezone.utc)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def metadata_url(model):
    return "https://api.open-meteo.com/data/" + MODEL_DOMAINS[model] + "/static/meta.json"


def receipt_identity(receipt):
    """Include acquisition time, request and response; never hash data alone."""
    if digest(receipt["data"]) != receipt["content_hash"]:
        raise ValueError("RUN_RECEIPT_HASH_MISMATCH")
    timestamp(receipt["received_at"])
    return digest(receipt)


def select_pair(model, metadata, now):
    now = timestamp(now)
    receipt_identity(metadata)
    if metadata["url"] != metadata_url(model) or metadata["params"] != {}:
        raise ValueError("RUN_METADATA_IDENTITY")
    data = metadata["data"]
    fields = ("last_run_initialisation_time", "last_run_availability_time")
    if any(not finite(data.get(k)) for k in fields):
        raise ValueError("RUN_METADATA_TIMES_MISSING")
    init, available = [datetime.fromtimestamp(data[k], timezone.utc) for k in fields]
    received = timestamp(metadata["received_at"])
    if not init <= available <= received <= now or (now-received).total_seconds() > 900:
        raise ValueError("RUN_METADATA_STALE_OR_FUTURE")
    # Provider documents eventually consistent servers and a ten-minute margin.
    if now < available + timedelta(minutes=10):
        raise ValueError("RUN_PROPAGATION_MARGIN")
    if init.minute or init.second or init.microsecond:
        raise ValueError("RUN_CYCLE_ALIGNMENT")
    if model != "gfs_hrrr" and init.hour % 6:
        raise ValueError("RUN_CYCLE_ALIGNMENT")
    # Only extended HRRR cycles can cover the full next standard day.
    current = init.replace(hour=init.hour // 6 * 6)
    if (now-current).total_seconds() > 24*3600:
        raise ValueError("RUN_TOO_OLD")
    return current, current - timedelta(hours=6)


def parameters(model, run, location):
    if model not in MODEL_DOMAINS:
        raise ValueError("RUN_UNSUPPORTED_MODEL")
    run = timestamp(run)
    if run.hour % 6 or run.minute or run.second or run.microsecond:
        raise ValueError("RUN_CYCLE_ALIGNMENT")
    return {"latitude": location["lat"], "longitude": location["lon"], "elevation": location["elev"],
            "models": model, "run": run.strftime("%Y-%m-%dT%H:%M"), "hourly": "temperature_2m",
            "forecast_days": 3, "temperature_unit": "fahrenheit", "timeformat": "unixtime", "timezone": "GMT"}


def verify_run(receipt, *, model, run, station, target, location, decision_at):
    """A successful exact-run fetch proves availability by receipt, not earlier."""
    now, run = timestamp(decision_at), timestamp(run)
    identity = receipt_identity(receipt)
    if receipt["url"] != ARCHIVE or receipt["params"] != parameters(model, run, location):
        raise ValueError("RUN_REQUEST_MISMATCH")
    received = timestamp(receipt["received_at"])
    start, end = standard_day(station, date.fromisoformat(target))
    if not run <= received <= now < start or run >= start:
        raise ValueError("RUN_NOT_KNOWN_BEFORE_DECISION")
    data = receipt["data"]
    if (data.get("utc_offset_seconds") != 0 or data.get("hourly_units", {}).get("temperature_2m") != "°F"
            or data.get("elevation") != location["elev"]
            or any(not finite(data.get(k)) or abs(data[k]-location[v]) > .5
                   for k,v in (("latitude", "lat"), ("longitude", "lon")))):
        raise ValueError("RUN_GRID_OR_UNITS")
    times, values = data["hourly"]["time"], data["hourly"]["temperature_2m"]
    if (len(times) != len(values) or any(type(t) is not int for t in times)
            or times != sorted(set(times))):
        raise ValueError("RUN_HOUR_ALIGNMENT")
    mapping = dict(zip(times, values))
    expected = [int(start.timestamp()) + 3600*i for i in range(24)]
    if any(not finite(mapping.get(t)) for t in expected):
        raise ValueError("RUN_INCOMPLETE_STANDARD_DAY")
    return {"model": model, "station": station, "target_date": target,
            "initialization_at": run.isoformat(), "received_at": received.isoformat(),
            "available_by": received.isoformat(), "availability_basis": "successful_exact_run_response",
            "exact_publication_time_verified": False, "known_before_decision": True,
            "run_age_hours": (now-run).total_seconds()/3600,
            "receipt_age_seconds": (now-received).total_seconds(), "receipt_id": identity,
            "standard_day_start": start.isoformat(), "standard_day_end": end.isoformat(),
            "grid": {k:data[k] for k in ("latitude", "longitude", "elevation")},
            "high_f": max(mapping[t] for t in expected), "low_f": min(mapping[t] for t in expected)}


def verified_pair(current, previous, *, model, runs, station, target, location, decision_at):
    if len(runs) != 2:
        raise ValueError("REVISION_REQUIRES_TWO_RUNS")
    new, old = [verify_run(r, model=model, run=t, station=station, target=target,
                           location=location, decision_at=decision_at) for r,t in zip((current, previous), runs)]
    if (timestamp(new["initialization_at"])-timestamp(old["initialization_at"]) != timedelta(hours=6)
            or new["grid"] != old["grid"]):
        raise ValueError("REVISION_REQUIRES_DISTINCT_MATCHED_RUNS")
    return {"current": new, "previous": old, "high_revision_f": new["high_f"]-old["high_f"],
            "low_revision_f": new["low_f"]-old["low_f"], "revision_kind": "exact_initialization_run_change",
            "first_seen_order_required": False}
