"""Separate retrospective GFS station-bias and uncertainty experiment.

No collection, market replay, deployment, or changes to the full-archive retest.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import scipy
from scipy.special import ndtr, ndtri

from backend.ml.weather_learning.contracts import CLI_CODES
from backend.ml.weather_learning.dataset import digest, utc, implementation_hash as frozen_hash
from backend.weather_historical_calibration import finite
from backend.weather_probability_screen import validate_history, write_once

VARIANTS = ("raw_gfs", "pooled_bias", "station_bias", "station_bias_and_uncertainty")
SPEC = {
    "version": "weather_station_distribution_v1", "model": "gfs_global",
    "run_policy": "previous calendar date at 00:00 UTC, local standard-day extrema",
    "start": "2026-04-03", "train_end": "2026-08-29",
    "cal_start": "2026-08-31", "cal_end": "2026-09-18",
    "test_start": "2026-09-20", "end": "2026-10-18",
    "stations": sorted(set(CLI_CODES.values())),
    "minimum_dates_per_station": {"train": 90, "calibration": 10, "test": 20},
    "bias_prior_station_days": 30.0, "variance_prior_station_days": 30.0,
    "sigma_floor_f": 1.5, "distribution": "Gaussian",
    "variance_estimator": "calibration mean squared residual about frozen training mean; no calibration recentering",
    "interval_coverages": [.5, .8, .9, .95],
    "variants": list(VARIANTS), "primary_candidate": "station_bias_and_uncertainty",
    "bootstrap_calendar_block_days": 7, "bootstrap_samples": 2000, "seed": 127,
    "selection": "All variants fixed before evaluation; no station selection or automatic promotion.",
}


def implementation_hash():
    files = [Path(__file__), Path(__file__).with_name("weather_probability_screen.py"),
             Path(__file__).with_name("weather_historical_calibration.py")]
    return digest({"files": {p.name: p.read_text(encoding="utf-8").replace("\r\n", "\n") for p in files},
                   "frozen_helpers": frozen_hash()})


def validate(rows, expected_hash, created_at):
    by_event = validate_history(rows, expected_hash)
    for r in by_event.values():
        if not all(finite(r.get(k)) for k in ("forecast_high_f", "forecast_low_f", "observed_high_f", "observed_low_f")):
            raise ValueError("INVALID_TEMPERATURE")
        if r["observed_low_f"] > r["observed_high_f"] or r["forecast_low_f"] > r["forecast_high_f"]:
            raise ValueError("INVALID_DAILY_EXTREMA")
        if (r.get("historical_availability") != "unverified"
                or r.get("label_snapshot_is_final_as_retrieved") is not True):
            raise ValueError("ARCHIVE_AVAILABILITY_MISLABELLED")
        if utc(r["retrieved_at"]) > utc(created_at):
            raise ValueError("INPUT_RECEIVED_AFTER_CREATION")
    return sorted(by_event.values(), key=lambda r: (r["target_date"], r["station"]))


def split(rows):
    parts = {"train": [r for r in rows if SPEC["start"] <= r["target_date"] <= SPEC["train_end"]],
             "calibration": [r for r in rows if SPEC["cal_start"] <= r["target_date"] <= SPEC["cal_end"]],
             "test": [r for r in rows if SPEC["test_start"] <= r["target_date"] <= SPEC["end"]]}
    for name, part in parts.items():
        for station in SPEC["stations"]:
            count = len({r["target_date"] for r in part if r["station"] == station})
            if count < SPEC["minimum_dates_per_station"][name]:
                raise ValueError(f"INSUFFICIENT_{name.upper()}_COVERAGE:{station}:{count}")
    return parts


def fit_metric(train, calibration, metric):
    """Estimate means on earlier dates and residual widths on later dates only."""
    if metric not in {"high", "low"} or not train or not calibration:
        raise ValueError("INVALID_TRAINING_PARTITION")
    if (max(r["target_date"] for r in train) > SPEC["train_end"]
            or any(not SPEC["cal_start"] <= r["target_date"] <= SPEC["cal_end"] for r in calibration)):
        raise ValueError("FITTING_DATE_LEAKAGE")
    for part in (train, calibration):
        if len({(r["station"], r["target_date"]) for r in part}) != len(part):
            raise ValueError("DUPLICATE_STATION_DATE")
    train_errors, cal_rows = defaultdict(list), defaultdict(list)
    for r in train:
        train_errors[r["station"]].append(r[f"observed_{metric}_f"] - r[f"forecast_{metric}_f"])
    for r in calibration:
        cal_rows[r["station"]].append(r)
    if set(train_errors) != set(cal_rows):
        raise ValueError("TRAIN_CALIBRATION_STATION_MISMATCH")
    # Each station/date contributes once; high/low models are reported separately.
    global_bias = float(np.mean([v for values in train_errors.values() for v in values]))
    biases = {s: float((sum(values) + SPEC["bias_prior_station_days"] * global_bias)
                       / (len(values) + SPEC["bias_prior_station_days"])) for s, values in train_errors.items()}
    variance = {}
    for name in ("raw_gfs", "pooled_bias", "station_bias"):
        residuals = [r[f"observed_{metric}_f"] - r[f"forecast_{metric}_f"]
                     - (0 if name == "raw_gfs" else global_bias if name == "pooled_bias" else biases[r["station"]])
                     for r in calibration]
        variance[name] = float(np.mean(np.square(residuals)))
    stations = {}
    for station, values in train_errors.items():
        residuals = [r[f"observed_{metric}_f"] - r[f"forecast_{metric}_f"] - biases[station] for r in cal_rows[station]]
        local_mse = float(np.mean(np.square(residuals)))
        prior = SPEC["variance_prior_station_days"]
        shrunk_variance = (len(residuals) * local_mse + prior * variance["station_bias"]) / (len(residuals) + prior)
        stations[station] = {"training_dates": len(values), "calibration_dates": len(residuals),
                             "unshrunk_training_bias_f": float(np.mean(values)), "bias_f": biases[station],
                             "calibration_residual_mse_f2": local_mse,
                             "sigma_f": max(SPEC["sigma_floor_f"], math.sqrt(shrunk_variance))}
    return {"metric": metric, "pooled_bias_f": global_bias, "stations": stations,
            "pooled_sigma_f": {name: max(SPEC["sigma_floor_f"], math.sqrt(v)) for name, v in variance.items()}}


def distribution(forecast_f, station, model, variant):
    """Pure retrospective distribution parameters; does not create a forecast receipt."""
    if variant not in VARIANTS or station not in model["stations"] or not finite(forecast_f):
        raise ValueError("UNSUPPORTED_FORECAST_DISTRIBUTION")
    local = model["stations"][station]
    correction = 0 if variant == "raw_gfs" else model["pooled_bias_f"] if variant == "pooled_bias" else local["bias_f"]
    sigma = local["sigma_f"] if variant == "station_bias_and_uncertainty" else model["pooled_sigma_f"][variant]
    return float(forecast_f + correction), sigma


def observation_scores(observed, mu, sigma):
    if not all(finite(x) for x in (observed, mu, sigma)) or sigma <= 0:
        raise ValueError("INVALID_GAUSSIAN_DISTRIBUTION")
    error = observed - mu
    z = error / sigma
    phi = math.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    # Gaussian CRPS is a proper score of the whole temperature distribution.
    crps = sigma * (z * (2 * ndtr(z) - 1) + 2 * phi - 1 / math.sqrt(math.pi))
    result = {"mae_f": abs(error), "squared_error_f2": error ** 2, "forecast_bias_f": -error,
              "crps_f": float(crps), "gaussian_nll": float(math.log(sigma * math.sqrt(2 * math.pi)) + z * z / 2)}
    for coverage in SPEC["interval_coverages"]:
        q = float(ndtri((1 + coverage) / 2))
        name = str(round(100 * coverage))
        result[f"coverage_{name}"] = float(abs(error) <= q * sigma)
        result[f"width_{name}_f"] = 2 * q * sigma
    return result


def summarize(samples):
    result = {k: float(np.mean([r[k] for r in samples])) for k in samples[0]}
    result["rmse_f"] = math.sqrt(result.pop("squared_error_f2"))
    return result


def paired_uncertainty(test, candidate, baseline):
    """Resample calendar blocks with all stations together; never individual rows."""
    blocks = defaultdict(list)
    origin = date.fromisoformat(SPEC["test_start"])
    for i, row in enumerate(test):
        block = (date.fromisoformat(row["target_date"]) - origin).days // SPEC["bootstrap_calendar_block_days"]
        blocks[block].append(i)
    rng = np.random.default_rng(SPEC["seed"])
    indices = list(blocks.values())
    result = {}
    for metric in ("mae_f", "crps_f"):
        differences = np.array([a[metric] - b[metric] for a, b in zip(candidate, baseline)])
        draws = []
        for _ in range(SPEC["bootstrap_samples"]):
            chosen = [i for block in rng.integers(0, len(indices), size=len(indices)) for i in indices[block]]
            draws.append(float(differences[chosen].mean()))
        result[metric] = {"candidate_minus_baseline": float(differences.mean()),
                          "bootstrap_95_percent_interval": np.quantile(draws, [.025, .975]).tolist()}
    return {"calendar_blocks": len(indices), "calendar_block_days": SPEC["bootstrap_calendar_block_days"],
            "scores": result, "interpretation": "Exploratory; few calendar blocks, no multiple-comparison or model-selection guarantee."}


def evaluate(test, models):
    samples = {metric: {variant: [observation_scores(r[f"observed_{metric}_f"],
                                                    *distribution(r[f"forecast_{metric}_f"], r["station"], models[metric], variant))
                                  for r in test] for variant in VARIANTS} for metric in ("high", "low")}
    result = {}
    for metric, variants in samples.items():
        result[metric] = {"overall": {name: summarize(values) for name, values in variants.items()},
                          "by_station": {s: {name: summarize([v for r, v in zip(test, values) if r["station"] == s])
                                               for name, values in variants.items()} for s in sorted({r["station"] for r in test})},
                          "paired_vs_pooled": paired_uncertainty(test, variants[SPEC["primary_candidate"]], variants["pooled_bias"]),
                          "paired_uncertainty_only": paired_uncertainty(test, variants[SPEC["primary_candidate"]], variants["station_bias"])}
    return result


def verify_model(model):
    if (model.get("model_id") != digest({k: v for k, v in model.items() if k != "model_id"})
            or model["identity"].get("implementation_hash") != implementation_hash()
            or model["identity"].get("spec_hash") != digest(SPEC)
            or model.get("production_training_eligible") is not False
            or model.get("historical_as_of_verified") is not False):
        raise ValueError("DISTRIBUTION_MODEL_IDENTITY_MISMATCH")


def run(history_directory, output):
    history_directory, output = Path(history_directory), Path(output)
    # This specification is immutable and saved before fitting or scoring.
    write_once(output / "spec.json", SPEC)
    raw_bytes = (history_directory / "dataset.json").read_bytes()
    status_bytes = (history_directory / "latest.json").read_bytes()
    status = json.loads(status_bytes)
    if status.get("status") not in {"complete", "complete_with_gaps"} or status.get("model") != SPEC["model"]:
        raise ValueError("GFS_HISTORY_NOT_COMPLETE")
    identity = {"spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
                "input_sha256": hashlib.sha256(raw_bytes).hexdigest(), "status_sha256": hashlib.sha256(status_bytes).hexdigest(),
                "numpy_version": np.__version__, "scipy_version": scipy.__version__}
    run_id = digest(identity)
    path = output / f"{run_id}.json"
    if path.exists():
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("identity") != identity or report.get("report_hash") != digest({k: v for k, v in report.items() if k != "report_hash"}):
            raise ValueError("EXISTING_DISTRIBUTION_REPORT_MISMATCH")
        model = json.loads((output / f"{report['model_id']}_model.json").read_text(encoding="utf-8"))
        verify_model(model)
        if model["model_id"] != report["model_id"] or model["identity"] != identity:
            raise ValueError("DISTRIBUTION_MODEL_IDENTITY_MISMATCH")
        return report
    created = datetime.now(timezone.utc).isoformat()
    rows = validate(json.loads(raw_bytes), status["dataset_hash"], created)
    parts = split(rows)
    models = {metric: fit_metric(parts["train"], parts["calibration"], metric) for metric in ("high", "low")}
    artifact = {"identity": identity, "created_at": created, "models": models,
                "train_hash": digest(parts["train"]), "calibration_hash": digest(parts["calibration"]),
                "mode": "retrospective_gfs_only", "production_training_eligible": False,
                "historical_as_of_verified": False, "live_ready": False, "paper_execution_enabled": False}
    artifact["model_id"] = digest(artifact)
    write_once(output / f"{artifact['model_id']}_model.json", artifact)
    coverage = {}
    windows = {"train": (SPEC["start"], SPEC["train_end"]), "calibration": (SPEC["cal_start"], SPEC["cal_end"]), "test": (SPEC["test_start"], SPEC["end"])}
    for name, part in parts.items():
        first, last = map(date.fromisoformat, windows[name])
        available = {(r["station"], r["target_date"]) for r in part}
        missing = [[s, (first + timedelta(days=d)).isoformat()] for s in SPEC["stations"] for d in range((last - first).days + 1)
                   if (s, (first + timedelta(days=d)).isoformat()) not in available]
        coverage[name] = {"station_dates": len(part), "dates": sorted({r["target_date"] for r in part}),
                          "by_station": {s: sum(r["station"] == s for r in part) for s in SPEC["stations"]},
                          "missing_station_dates": missing}
    report = {"run_id": run_id, "identity": identity, "spec": SPEC, "created_at": created,
              "model_id": artifact["model_id"], "status": "retrospective_comparison_complete_no_promotion",
              "source_station_dates": len(rows), "coverage": coverage,
              "unscored_buffer_or_outside_dates": sorted({r["target_date"] for r in rows} - {r["target_date"] for p in parts.values() for r in p}),
              "evaluation": evaluate(parts["test"], models), "parameters": models,
              "test_used_for_fitting_or_selection": False, "profitability_evaluated": False, "deployment_changed": False,
              "limitations": ["Single 00 UTC GFS run per target date; not the unfinished three-model, four-decision-hour archive.",
                              "Historical public availability and label publication timing are unverified; no forward evidence.",
                              "Retrospective months have been inspected in earlier experiments, not a permanently unseen holdout.",
                              "Gaussian shape and stable station bias can fail under regime changes; interval coverage is explicitly tested.",
                              "High/low and stations share weather; sample counts are station/date events and calendar dates.",
                              "The uncertainty assigned to raw GFS is calibrated here; raw deterministic GFS has no supplied distribution.",
                              "Temperature/CRPS improvement is not market outperformance or executable profit."]}
    report["report_hash"] = digest(report)
    write_once(path, report)
    return report
