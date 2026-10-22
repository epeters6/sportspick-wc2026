"""Offline rolling replay of station bias and genuinely earlier prediction errors.

Historical publication times are unknown. The maturity lag is an assumption,
not proof of as-of eligibility. Nothing here writes operational forecasts.
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

from backend.ml.weather_learning.dataset import digest
from backend.weather_probability_screen import write_once
from backend import weather_station_distribution as static

VARIANTS = ("raw_gfs", "pr18_static", "rolling_bias_static_width", "rolling_bias_prequential_width")
SPEC = {
    "version": "weather_adaptive_calibration_v1",
    "model": "gfs_global", "start": "2026-04-03", "end": "2026-10-18",
    "run_policy": "previous calendar date at 00 UTC, local standard-day extrema",
    "test_start": "2026-09-20", "test_end": "2026-10-18",
    "stations": static.SPEC["stations"],
    "assumed_label_lag_target_days": 3,
    "bias_window_calendar_days": 45, "bias_prior_station_days": 15.0,
    "minimum_bias_dates_per_station": 30,
    "error_window_calendar_days": 60, "variance_prior_station_days": 30.0,
    "minimum_error_dates_per_station": 30,
    "sigma_floor_f": 1.5, "distribution": "Gaussian",
    "variance_estimator": "Recent squared errors of earlier rolling-mean predictions, no residual recentering",
    "variants": list(VARIANTS), "primary_candidate": "rolling_bias_prequential_width",
    "interval_coverages": static.SPEC["interval_coverages"],
    "bootstrap_calendar_block_days": 7, "bootstrap_samples": 2000, "seed": 127,
    "selection": "Fixed windows and priors; report every variant and station, no tuning or promotion",
    "evaluation_type": "Exploratory rolling-origin replay; earlier evaluation labels can train later predictions",
    "historical_as_of_verified": False,
}


def implementation_hash():
    return digest({"source": Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n"),
                   "static_comparator_and_helpers": static.implementation_hash()})


def window(rows, cutoff, days):
    first = (date.fromisoformat(cutoff) - timedelta(days=days - 1)).isoformat()
    return [r for r in rows if first <= r["target_date"] <= cutoff]


def grouped_values(rows, value):
    groups = defaultdict(list)
    for row in rows:
        groups[row["station"]].append(value(row))
    return groups


def enough(groups, stations, minimum):
    return all(len(groups[s]) >= minimum for s in stations)


def fit_day(rows, earlier_errors, target, metric, stations):
    """Fit only earlier, assumed-mature events. No current target outcome is used."""
    cutoff = (date.fromisoformat(target) - timedelta(days=SPEC["assumed_label_lag_target_days"])).isoformat()
    train = window(rows, cutoff, SPEC["bias_window_calendar_days"])
    groups = grouped_values(train, lambda r: r[f"observed_{metric}_f"] - r[f"forecast_{metric}_f"])
    if not enough(groups, stations, SPEC["minimum_bias_dates_per_station"]):
        return None
    pooled_bias = float(np.mean([e for values in groups.values() for e in values]))
    prior = SPEC["bias_prior_station_days"]
    biases = {s: float((sum(groups[s]) + prior * pooled_bias) / (len(groups[s]) + prior)) for s in stations}
    residuals = window(earlier_errors, cutoff, SPEC["error_window_calendar_days"])
    # Each error was made by a mean model that itself excluded that target label.
    for r in residuals:
        if r["label_target_cutoff"] > (date.fromisoformat(r["target_date"]) - timedelta(days=SPEC["assumed_label_lag_target_days"])).isoformat():
            raise ValueError("PREQUENTIAL_ERROR_TIMING_MISMATCH")
    error_groups = grouped_values(residuals, lambda r: r["error_f"] ** 2)
    uncertainty = enough(error_groups, stations, SPEC["minimum_error_dates_per_station"])
    pooled_mse = float(np.mean([e for values in error_groups.values() for e in values])) if uncertainty else None
    stations_fit = {}
    for station in stations:
        count = len(error_groups[station])
        variance = ((sum(error_groups[station]) + SPEC["variance_prior_station_days"] * pooled_mse)
                    / (count + SPEC["variance_prior_station_days"])) if uncertainty else None
        stations_fit[station] = {"bias_f": biases[station], "bias_dates": len(groups[station]),
                                 "error_dates": count,
                                 "sigma_f": max(SPEC["sigma_floor_f"], math.sqrt(variance)) if uncertainty else None}
    return {"target_date": target, "metric": metric, "label_target_cutoff": cutoff,
            "latest_training_target": max(r["target_date"] for r in train),
            "latest_error_target": max((r["target_date"] for r in residuals), default=None),
            "bias_data_hash": digest(train), "error_data_hash": digest(residuals),
            "pooled_bias_f": pooled_bias, "pooled_prequential_mse_f2": pooled_mse,
            "uncertainty_ready": uncertainty, "stations": stations_fit}


def replay(rows):
    """Generate historical simulations in date order, retaining every fitted state."""
    events = [(r["station"], r["target_date"]) for r in rows]
    if len(events) != len(set(events)):
        raise ValueError("DUPLICATE_STATION_DATE")
    stations = sorted({r["station"] for r in rows})
    by_date = defaultdict(list)
    for row in rows:
        by_date[row["target_date"]].append(row)
    models, predictions = {}, []
    for metric in ("high", "low"):
        earlier_errors = []
        for target, today in sorted(by_date.items()):
            model = fit_day(rows, earlier_errors, target, metric, stations)
            if model is None:
                if SPEC["test_start"] <= target <= SPEC["test_end"]:
                    raise ValueError("INSUFFICIENT_ROLLING_BIAS_HISTORY")
                continue
            model_id = digest(model)
            models[model_id] = model
            for row in sorted(today, key=lambda r: r["station"]):
                local = model["stations"][row["station"]]
                mu = row[f"forecast_{metric}_f"] + local["bias_f"]
                if SPEC["test_start"] <= target <= SPEC["test_end"]:
                    if not model["uncertainty_ready"]:
                        raise ValueError("INSUFFICIENT_PREQUENTIAL_ERROR_HISTORY")
                    predictions.append({"station": row["station"], "target_date": target,
                                        "metric": metric, "model_id": model_id,
                                        "mean_f": mu, "sigma_f": local["sigma_f"],
                                        "run_initialization_utc": row["run_initialization_utc"]})
                # Storing a residual now does not make it eligible now. fit_day
                # filters it by the same assumed maturity lag on subsequent days.
                earlier_errors.append({"station": row["station"], "target_date": target,
                                       "label_target_cutoff": model["label_target_cutoff"],
                                       "model_id": model_id, "predicted_mean_f": mu,
                                       "error_f": row[f"observed_{metric}_f"] - mu})
    return {"models": models, "predictions": predictions}


def evaluate(test, evidence, comparators):
    predicted = {(p["metric"], p["station"], p["target_date"]): p for p in evidence["predictions"]}
    if len(predicted) != 2 * len(test) or len(predicted) != len(evidence["predictions"]):
        raise ValueError("PREDICTION_COHORT_MISMATCH")
    result = {}
    for metric in ("high", "low"):
        scores = {v: [] for v in VARIANTS}
        for row in test:
            p = predicted[metric, row["station"], row["target_date"]]
            raw = static.distribution(row[f"forecast_{metric}_f"], row["station"], comparators[metric], "raw_gfs")
            old = static.distribution(row[f"forecast_{metric}_f"], row["station"], comparators[metric], "station_bias_and_uncertainty")
            distributions = {"raw_gfs": raw, "pr18_static": old,
                             "rolling_bias_static_width": (p["mean_f"], old[1]),
                             "rolling_bias_prequential_width": (p["mean_f"], p["sigma_f"])}
            for name, params in distributions.items():
                scores[name].append(static.observation_scores(row[f"observed_{metric}_f"], *params))
        result[metric] = {
            "overall": {v: static.summarize(values) for v, values in scores.items()},
            "by_station": {s: {v: static.summarize([x for r, x in zip(test, values) if r["station"] == s])
                                for v, values in scores.items()} for s in sorted({r["station"] for r in test})},
            "paired_vs_pr18": static.paired_uncertainty(test, scores[SPEC["primary_candidate"]], scores["pr18_static"]),
            "paired_vs_raw": static.paired_uncertainty(test, scores[SPEC["primary_candidate"]], scores["raw_gfs"]),
            "paired_width_only": static.paired_uncertainty(test, scores[SPEC["primary_candidate"]], scores["rolling_bias_static_width"]),
        }
    return result


def verify_evidence(evidence, identity):
    if (evidence.get("artifact_id") != digest({k: v for k, v in evidence.items() if k != "artifact_id"})
            or evidence.get("identity") != identity
            or evidence.get("historical_as_of_verified") is not False
            or evidence.get("production_training_eligible") is not False
            or evidence.get("live_ready") is not False
            or evidence.get("paper_execution_enabled") is not False):
        raise ValueError("ADAPTIVE_EVIDENCE_IDENTITY_MISMATCH")
    for model_id, model in evidence["models"].items():
        if digest(model) != model_id:
            raise ValueError("ADAPTIVE_MODEL_IDENTITY_MISMATCH")
    for p in evidence["predictions"]:
        model = evidence["models"].get(p["model_id"])
        if not model or model["target_date"] != p["target_date"] or model["metric"] != p["metric"]:
            raise ValueError("ADAPTIVE_PREDICTION_MODEL_MISMATCH")


def run(history_directory, output):
    history_directory, output = Path(history_directory), Path(output)
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
            raise ValueError("EXISTING_ADAPTIVE_REPORT_MISMATCH")
        evidence = json.loads((output / f"{report['artifact_id']}_replay.json").read_text(encoding="utf-8"))
        verify_evidence(evidence, identity)
        if evidence["artifact_id"] != report["artifact_id"]:
            raise ValueError("ADAPTIVE_EVIDENCE_IDENTITY_MISMATCH")
        return report
    created = datetime.now(timezone.utc).isoformat()
    rows = static.validate(json.loads(raw_bytes), status["dataset_hash"], created)
    rows = [r for r in rows if SPEC["start"] <= r["target_date"] <= SPEC["end"]]
    parts = static.split(rows)
    comparators = {m: static.fit_metric(parts["train"], parts["calibration"], m) for m in ("high", "low")}
    evidence = {**replay(rows), "comparators": comparators, "identity": identity,
                "created_at": created, "mode": "retrospective_rolling_replay",
                "historical_as_of_verified": False, "production_training_eligible": False,
                "live_ready": False, "paper_execution_enabled": False}
    evidence["artifact_id"] = digest(evidence)
    verify_evidence(evidence, identity)
    # Every simulated prediction and its fitted state is persisted before scoring.
    write_once(output / f"{evidence['artifact_id']}_replay.json", evidence)
    test = parts["test"]
    available = {(r["station"], r["target_date"]) for r in rows}
    expected_dates = [(date.fromisoformat(SPEC["start"]) + timedelta(days=d)).isoformat()
                      for d in range((date.fromisoformat(SPEC["end"]) - date.fromisoformat(SPEC["start"])).days + 1)]
    report = {"run_id": run_id, "identity": identity, "spec": SPEC, "created_at": created,
              "artifact_id": evidence["artifact_id"], "status": "exploratory_rolling_comparison_complete_no_promotion",
              "coverage": {"source_station_dates": len(rows), "test_station_dates": len(test),
                           "test_dates": sorted({r["target_date"] for r in test}),
                           "test_by_station": {s: sum(r["station"] == s for r in test) for s in SPEC["stations"]},
                           "missing_station_dates": [[s, d] for d in expected_dates for s in SPEC["stations"] if (s, d) not in available]},
              "evaluation": evaluate(test, evidence, comparators),
              "earlier_evaluation_labels_used_for_later_fits": True,
              "current_or_future_target_labels_used": False, "parameters_tuned_on_evaluation": False,
              "profitability_evaluated": False, "deployment_changed": False,
              "limitations": ["Reused retrospective months; not a fresh holdout or genuinely forward evidence.",
                              "Three-target-day label maturity is assumed. Historical forecast/label publication times are unverified.",
                              "Only one original GFS run per target; the full multi-model decision archive remains separate.",
                              "Gaussian tails and rolling windows can still miss sudden regime changes; coverage is measured, not guaranteed.",
                              "Seven-day block intervals ignore fitting/model-choice uncertainty and have only five test blocks.",
                              "Station/date events share weather; high and low are not additional independent observations.",
                              "Temperature accuracy and distribution scores do not establish an advantage over market prices or profit."]}
    report["report_hash"] = digest(report)
    write_once(path, report)
    return report
