"""Explicit feature allowlist and complete, officially labelled event vectors."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from . import SOURCE

TRAINING_SOURCES = ("weather_forward_v2", SOURCE)
WEATHER_FEATURES = (
    "forecast_mean_f", "model_disagreement_f", "gfs_high_f", "hrrr_high_f", "ecmwf_high_f",
    "gfs_low_f", "hrrr_low_f", "ecmwf_low_f", "nowcast_mean_f", "nowcast_probability",
    "observed_high_f", "observed_low_f", "observed_temp_f", "temperature_trend_f_hour",
    "observation_age_minutes", "observation_count", "day_coverage_fraction",
    "dewpoint_f", "wind_speed_kt", "wind_sin", "wind_cos", "cloud_cover_pct",
    "neighbor_temperature_delta_f", "neighbor_trend_f_hour", "model_count",
    "remaining_hours", "forecast_age_minutes", "nowcast_adjustment_f",
)


def utc(value) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return result.astimezone(timezone.utc)


def finite(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def implementation_hash():
    directory = Path(__file__).parent
    return digest({path.name: path.read_text(encoding="utf-8").replace("\r\n", "\n") for path in sorted(directory.glob("*.py"))})


def vector_key(row: dict) -> tuple:
    m = row["metadata"]
    return (m["platform"], m["station"], m["target_date"], m["metric"], m["run_id"])


def event_key(row: dict) -> tuple:
    """Cross-venue forecasts of the same station-day are not independent."""
    m = row["metadata"]
    return (m["station"], m["target_date"], m["metric"])


def partition_valid(rows: list[dict], *, labelled: bool = False) -> bool:
    if len(rows) < 2 or len({r["outcome"] for r in rows}) != len(rows):
        return False
    bounds = sorted(((-math.inf if r["metadata"].get("bucket_low_f") is None else finite(r["metadata"]["bucket_low_f"], math.nan)),
                     (math.inf if r["metadata"].get("bucket_high_f") is None else finite(r["metadata"]["bucket_high_f"], math.nan))) for r in rows)
    if bounds[0][0] != -math.inf or bounds[-1][1] != math.inf:
        return False
    if any(not lo < hi for lo, hi in bounds) or any(abs(bounds[i][1] - bounds[i + 1][0]) > 1e-6 for i in range(len(bounds) - 1)):
        return False
    if labelled and (any(type(r.get("is_correct")) is not bool for r in rows) or sum(r["is_correct"] for r in rows) != 1):
        return False
    return True


def features(row: dict) -> dict:
    """Never include outcomes, settlement values, selection flags or future weather."""
    m = row["metadata"]
    stamp = utc(m.get("decision_at") or row["created_at"])
    local = stamp.astimezone(ZoneInfo(m["observation_timezone"]))
    raw = finite(m.get("raw_model_prob"), finite(row.get("prob"), 0.5))
    market = finite(m.get("market_vector_prob"), finite(row.get("market_price"), 0.5))
    lo, hi = finite(m.get("bucket_low_f")), finite(m.get("bucket_high_f"))
    result = {
        "station": m["station"], "venue": m["platform"], "metric": m["metric"],
        "authority": m.get("settlement_source", "unknown"),
        "raw_probability": raw, "market_probability": market,
        "raw_logit": math.log(max(1e-5, raw) / max(1e-5, 1 - raw)),
        "market_logit": math.log(max(1e-5, market) / max(1e-5, 1 - market)),
        "lead_days": finite(m.get("lead_days"), 0),
        "hour": local.hour + local.minute / 60,
        "season_sin": math.sin(2 * math.pi * local.timetuple().tm_yday / 365.25),
        "season_cos": math.cos(2 * math.pi * local.timetuple().tm_yday / 365.25),
        "lower_tail": float(lo is None), "upper_tail": float(hi is None),
        "lower_bound_f": lo if lo is not None else (hi or 0) - 10,
        "upper_bound_f": hi if hi is not None else (lo or 0) + 10,
        "residual_sigma_f": finite(m.get("residual_sigma_f"), 0),
    }
    weather = m.get("weather_features") or {}
    for key in WEATHER_FEATURES:
        value = finite(weather.get(key))
        result[key] = value if value is not None else 0.0
        result[key + "_missing"] = float(value is None)
    return result


def prepare(rows: list[dict], as_of: datetime) -> tuple[list[dict], dict]:
    """Drop whole invalid vectors; never invent losing labels for ungraded buckets."""
    as_of = utc(as_of)
    groups = defaultdict(list)
    rejected = Counter()
    for row in rows:
        try:
            m = row["metadata"]
            if row.get("source") not in TRAINING_SOURCES or m.get("station_verified") is not True:
                raise ValueError("UNVERIFIED_SOURCE_OR_STATION")
            key = vector_key(row)
            groups[key].append(row)
        except (KeyError, TypeError, ValueError):
            rejected["UNVERIFIED_SOURCE_OR_STATION"] += 1
    kept = []
    for group in groups.values():
        try:
            if not partition_valid(group, labelled=True):
                raise ValueError("INCOMPLETE_OR_CONFLICTING_LABEL_VECTOR")
            for row in group:
                m = row["metadata"]
                decision = utc(m.get("decision_at") or row["created_at"])
                resolved = utc(row["resolved_at"])
                if m.get("label_source") != "venue_official" or not decision < resolved <= as_of:
                    raise ValueError("LABEL_NOT_OFFICIAL_OR_NOT_AVAILABLE")
                if m.get("official_market_id") != row["outcome"]:
                    raise ValueError("OFFICIAL_MARKET_ID_MISMATCH")
                if m.get("official_result") not in ("yes", "no") or row["is_correct"] != (m["official_result"] == "yes"):
                    raise ValueError("OFFICIAL_RESULT_MISMATCH")
                for name, value in (("prob", row.get("prob")), ("raw_model_prob", m.get("raw_model_prob"))):
                    if finite(value) is None or not 0 <= float(value) <= 1:
                        raise ValueError("INVALID_PROBABILITY:" + name)
                if row["source"] == SOURCE:
                    if any(utc(m[name]) > decision for name in ("weather_forecast_received_at", "observations_received_at")):
                        raise ValueError("WEATHER_RECEIVED_AFTER_DECISION")
                    if utc(m["label_available_at"]) != resolved:
                        raise ValueError("LABEL_AVAILABILITY_MISMATCH")
                if decision.astimezone(ZoneInfo(m["observation_timezone"])).date().isoformat() > m["target_date"]:
                    raise ValueError("AFTER_OBSERVATION_DAY")
                probability = finite(m.get("market_vector_prob"))
                if probability is None or not 0 <= probability <= 1:
                    raise ValueError("INVALID_MARKET_BASELINE")
                features(row)
            kept.extend(group)
        except (KeyError, TypeError, ValueError) as exc:
            rejected[str(exc)] += len(group)
    kept.sort(key=lambda r: (r["metadata"]["target_date"], r["created_at"], r["id"]))
    return kept, {"input_rows": len(rows), "accepted_rows": len(kept), "rejected_rows_by_reason": dict(rejected),
                  "station_days": len({event_key(r) for r in kept}),
                  "distinct_dates": len({r["metadata"]["target_date"] for r in kept}),
                  "dataset_hash": digest(kept)}


def load_history(db, *, since: str | None = None, page_size: int = 500) -> list[dict]:
    rows = []
    for source in TRAINING_SOURCES:
        offset = 0
        while True:
            query = db.table("model_predictions").select("*").eq("source", source)
            if since:
                query = query.gte("created_at", since)
            page = query.order("created_at").order("id").range(offset, offset + page_size - 1).execute().data or []
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
    return rows
