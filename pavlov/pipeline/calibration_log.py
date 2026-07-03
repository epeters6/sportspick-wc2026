"""
pipeline/calibration_log.py – Rich forecast error logging and adaptive calibration.

Goes beyond binary win/loss outcomes to extract maximum learning from every
market the bot sees:

  - Records ensemble / NWS / OWM forecast errors each time a market resolves
  - Computes adaptive sigma per (city, metric) from rolling error history
    so the Gaussian fallback uses *measured* uncertainty rather than guesses
  - Tracks per-source skill so we can see which forecast to trust
  - Shared by both placed-bet and skipped-signal pathways

Public API
----------
record_resolution(city, metric, date, actual_temp_f, ensemble_mean=…, …)
get_adaptive_sigma(city, metric, default_f) -> float
get_source_skill(city, metric) -> dict
load_records() -> list[dict]
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import data_paths

logger = logging.getLogger(__name__)

_ERRORS_FILE   = os.path.join(data_paths.logs_dir(), "forecast_errors.json")

_tls = threading.local()


def _active_errors_file() -> str:
    path = getattr(_tls, "forecast_errors_file", None)
    return path if path else _ERRORS_FILE


@contextmanager
def use_forecast_errors_file(path: str):
    """Temporarily read/write a different forecast_errors.json (e.g. Polymarket)."""
    prev = getattr(_tls, "forecast_errors_file", None)
    _tls.forecast_errors_file = path
    try:
        yield
    finally:
        if prev is None:
            if hasattr(_tls, "forecast_errors_file"):
                delattr(_tls, "forecast_errors_file")
        else:
            _tls.forecast_errors_file = prev

# Cap the rolling window — older records are dropped on save.
_MAX_RECORDS   = 1000

# Adaptive sigma needs at least this many records for a (city, metric)
# before it overrides the hardcoded default.
_MIN_RECORDS   = 8

# Below this many records, we blend measured stdev with the hardcoded default
# to avoid wild swings on early data.  Above 30, we trust the measurement fully.
_FULL_TRUST_AT = 30


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_records() -> list[dict]:
    path = _active_errors_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_records(records: list[dict]) -> None:
    path = _active_errors_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if len(records) > _MAX_RECORDS:
        records = records[-_MAX_RECORDS:]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_resolution(
    city: str,
    metric: str,
    date_str: str,
    actual_temp_f: float,
    ensemble_mean: float | None = None,
    ensemble_spread: float | None = None,
    nws_predicted: float | None = None,
    owm_predicted: float | None = None,
) -> bool:
    """Append a forecast resolution to forecast_errors.json.

    Deduplicates on (city, metric, date_str): if a record for the same
    market day already exists, returns False without saving.

    Returns True if a new record was appended.
    """
    if not (city and metric and date_str) or actual_temp_f is None:
        return False

    records = load_records()
    key     = (city.lower(), metric.lower(), date_str)
    for r in records:
        if (
            r.get("city", "").lower() == key[0]
            and r.get("metric", "").lower() == key[1]
            and r.get("date") == key[2]
        ):
            return False

    rec: dict = {
        "logged_at":     datetime.now(timezone.utc).isoformat(),
        "date":          date_str,
        "city":          city,
        "metric":        metric,
        "actual_temp_f": round(float(actual_temp_f), 1),
    }

    if ensemble_mean is not None:
        rec["ensemble_mean"]  = round(float(ensemble_mean), 1)
        rec["error_ensemble"] = round(float(ensemble_mean) - float(actual_temp_f), 2)
    if ensemble_spread is not None:
        rec["ensemble_spread"] = round(float(ensemble_spread), 1)
    if nws_predicted is not None:
        rec["nws_predicted"]  = round(float(nws_predicted), 1)
        rec["error_nws"]      = round(float(nws_predicted) - float(actual_temp_f), 2)
    if owm_predicted is not None:
        rec["owm_predicted"]  = round(float(owm_predicted), 1)
        rec["error_owm"]      = round(float(owm_predicted) - float(actual_temp_f), 2)

    records.append(rec)
    _save_records(records)

    logger.info(
        "CalibrationLog: recorded %s %s on %s — actual=%.1f°F  "
        "errors: ens=%s nws=%s owm=%s",
        city, metric, date_str, actual_temp_f,
        rec.get("error_ensemble", "—"),
        rec.get("error_nws", "—"),
        rec.get("error_owm", "—"),
    )
    return True


def get_adaptive_sigma(
    city: str,
    metric: str,
    default_f: float,
) -> float:
    """Return rolling forecast-error stdev for (city, metric), or *default_f*.

    Uses ensemble_mean errors when available.  Below MIN_RECORDS history we
    return *default_f* unchanged.  Between MIN_RECORDS and FULL_TRUST_AT we
    blend measured stdev with the default so the value transitions smoothly.

    The measured value is bounded to [1.0, 6.0] °F to guard against single
    catastrophic forecast outliers swinging the sigma table.
    """
    records = load_records()
    errors = [
        float(r["error_ensemble"])
        for r in records
        if r.get("city") == city
        and r.get("metric") == metric
        and "error_ensemble" in r
    ]

    n = len(errors)
    if n < _MIN_RECORDS:
        return default_f

    try:
        measured = statistics.stdev(errors)
    except statistics.StatisticsError:
        return default_f

    measured = max(1.0, min(6.0, measured))
    blend    = min(n / _FULL_TRUST_AT, 1.0)
    sigma    = measured * blend + default_f * (1.0 - blend)
    return round(sigma, 2)


def get_source_skill(city: str, metric: str) -> dict:
    """Return mean absolute forecast error per source for (city, metric).

    Lower values = better forecast skill.  Useful for auditing whether NWS,
    OWM, or the ensemble has been most accurate for a given city.
    """
    records = load_records()
    relevant = [
        r for r in records
        if r.get("city") == city and r.get("metric") == metric
    ]

    out: dict = {"records": len(relevant)}
    for src_key, label in (
        ("error_ensemble", "ensemble"),
        ("error_nws",      "nws"),
        ("error_owm",      "owm"),
    ):
        errs = [abs(float(r[src_key])) for r in relevant if src_key in r]
        if errs:
            out[label] = {
                "mae":    round(sum(errs) / len(errs), 2),
                "n":      len(errs),
            }
    return out
