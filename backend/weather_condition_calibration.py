"""Offline range-conditioned temperature means and asymmetric uncertainty.

Exploratory replay only: archive availability and label maturity are unverified.
This module is deliberately outside the frozen v3 and daytime paper models.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import scipy

from backend.ml.weather_learning.dataset import digest
from backend.weather_probability_screen import write_once
from backend import weather_adaptive_calibration as rolling
from backend import weather_residual_calibration as residual
from backend import weather_station_distribution as static

VARIANTS = ("reference", "conditional_mean", "conditional_asymmetric")
SPEC = {
    "version": "weather_condition_calibration_v1", "model": "gfs_global",
    "start": "2026-04-03", "end": "2026-10-18", "test_start": "2026-09-20",
    "stations": static.SPEC["stations"], "assumed_label_lag_target_days": 3,
    "mean_window_days": 60, "minimum_mean_dates_per_station": 30,
    "feature": "(forecast high minus forecast low minus 15 F) / 10 F",
    "pooled_slope_ridge": 10.0, "station_intercept_prior": 15.0, "station_slope_prior": 60.0,
    "error_window_days": 60, "minimum_error_dates_per_station": 30,
    "range_bands_f": [10, 20], "minimum_band_events": 30,
    "uncertainty_station_prior": 30.0, "kernel_sigma_f": 1.0, "error_bin_width_f": 1.0,
    "uncertainty": "Signed earlier out-of-sample errors, conditioned on forecast range; station/pool shrinkage",
    "reference": {"high": "PR20 mean recentering with PR19 width", "low": "PR19 mean and width"},
    "variants": list(VARIANTS), "primary": {"high": "conditional_mean", "low": "conditional_asymmetric"},
    "selection": "Fixed specification before this replay; report all variants, stations and range bands; no promotion",
    "evaluation_type": "Exploratory repeated-month rolling replay; range hypothesis came from inspected test months",
    "historical_as_of_verified": False,
}


def implementation_hash():
    return digest({"source": Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n"),
                   "helpers": residual.implementation_hash()})


def span(row):
    value = float(row["forecast_high_f"]) - float(row["forecast_low_f"])
    if not np.isfinite(value) or value < 0:
        raise ValueError("INVALID_FORECAST_RANGE")
    return value


def band(row):
    return int(span(row) >= 10) + int(span(row) >= 20)


def cutoff(target):
    return (date.fromisoformat(target) - timedelta(days=SPEC["assumed_label_lag_target_days"])).isoformat()


def fit_mean(rows, target, metric, stations):
    train = rolling.window(rows, cutoff(target), SPEC["mean_window_days"])
    if len({(r["station"], r["target_date"]) for r in train}) != len(train):
        raise ValueError("DUPLICATE_TRAINING_EVENT")
    if any(sum(r["station"] == s for r in train) < SPEC["minimum_mean_dates_per_station"] for s in stations):
        return None
    x = np.array([(span(r) - 15) / 10 for r in train])
    y = np.array([r[f"observed_{metric}_f"] - r[f"forecast_{metric}_f"] for r in train])
    # A pooled intercept/slope plus regularized station deviations. All priors
    # are fixed; no coefficient, station or window is selected on test scores.
    matrix = np.zeros((len(train), 2 + 2 * len(stations)))
    matrix[:, 0], matrix[:, 1] = 1, x
    for i, station in enumerate(stations):
        own = np.array([r["station"] == station for r in train])
        matrix[:, 2 + 2*i], matrix[:, 3 + 2*i] = own, own * x
    penalty = np.diag([0, SPEC["pooled_slope_ridge"]] +
                      [SPEC["station_intercept_prior"], SPEC["station_slope_prior"]] * len(stations))
    coefficients = np.linalg.solve(matrix.T @ matrix + penalty, matrix.T @ y)
    if not np.isfinite(coefficients).all():
        raise ValueError("INVALID_MEAN_FIT")
    return {"target_date": target, "metric": metric, "label_target_cutoff": cutoff(target),
            "latest_training_target": max(r["target_date"] for r in train), "training_hash": digest(train),
            "training_events": len(train), "stations": {
                s: {"intercept_f": float(coefficients[0] + coefficients[2+2*i]),
                    "range_slope_f": float(coefficients[1] + coefficients[3+2*i])}
                for i, s in enumerate(stations)}}


def mean(row, model):
    local = model["stations"][row["station"]]
    return float(row[f"forecast_{model['metric']}_f"] + local["intercept_f"] +
                 local["range_slope_f"] * (span(row) - 15) / 10)


def fit_errors(errors, target, metric, stations):
    selected = rolling.window(errors, cutoff(target), SPEC["error_window_days"])
    if len({(r["station"], r["target_date"]) for r in selected}) != len(selected):
        raise ValueError("DUPLICATE_CALIBRATION_EVENT")
    if any(r["label_target_cutoff"] != cutoff(r["target_date"]) for r in selected):
        raise ValueError("CALIBRATION_TARGET_LEAKAGE")
    if any(sum(r["station"] == s for r in selected) < SPEC["minimum_error_dates_per_station"] for s in stations):
        return None
    return {"target_date": target, "metric": metric, "label_target_cutoff": cutoff(target), "errors": selected}


def distribution(model, station, range_band):
    errors = [r for r in model["errors"] if r["range_band"] == range_band]
    backoff = len(errors) < SPEC["minimum_band_events"]
    if backoff:
        errors = model["errors"]
    if not errors:
        raise ValueError("NO_CALIBRATION_ERRORS")
    own = np.array([r["station"] == station for r in errors], dtype=float)
    prior = SPEC["uncertainty_station_prior"]
    weights = (own + prior / len(errors)) / (own.sum() + prior)
    # Fixed one-degree compression makes proper-score evaluation bounded, while
    # preserving the signed tails instead of forcing symmetric Gaussian errors.
    bins = np.rint(np.array([r["error_f"] for r in errors]) / SPEC["error_bin_width_f"]) * SPEC["error_bin_width_f"]
    centers = np.unique(bins)
    mass = np.array([weights[bins == c].sum() for c in centers])
    return {"centers_f": centers.tolist(), "weights": mass.tolist(),
            "kernel_sigma_f": SPEC["kernel_sigma_f"], "band_backoff": backoff,
            "calibration_events": len(errors), "station_events": int(own.sum())}


def replay(rows):
    if len({(r["station"], r["target_date"]) for r in rows}) != len(rows):
        raise ValueError("DUPLICATE_STATION_DATE")
    baseline = residual.replay(rows)
    references = {(p["metric"], p["station"], p["target_date"]): p for p in baseline["predictions"]}
    stations = sorted({r["station"] for r in rows})
    by_date = {}
    for row in rows:
        by_date.setdefault(row["target_date"], []).append(row)
    models, error_models, predictions = {}, {}, []
    for metric in ("high", "low"):
        errors = []
        for target, today in sorted(by_date.items()):
            model = fit_mean(rows, target, metric, stations)
            if model is None:
                continue
            model_id = digest(model)
            models[model_id] = model
            if SPEC["test_start"] <= target <= SPEC["end"]:
                uncertainty = fit_errors(errors, target, metric, stations)
                if uncertainty is None:
                    raise ValueError("INSUFFICIENT_CONDITIONAL_ERROR_HISTORY")
                error_id = digest(uncertainty)
                error_models[error_id] = uncertainty
                for row in sorted(today, key=lambda r: r["station"]):
                    reference = references[metric, row["station"], target]
                    predictions.append({"station": row["station"], "target_date": target, "metric": metric,
                        "model_id": model_id, "error_model_id": error_id,
                        "run_initialization_utc": row["run_initialization_utc"], "range_band": band(row),
                        "mean_f": mean(row, model), "sigma_f": reference["parent_sigma_f"],
                        "reference_mean_f": reference["mean_f"] if metric == "high" else reference["parent_mean_f"],
                        "reference_model_id": reference["model_id"],
                        "distribution": distribution(uncertainty, row["station"], band(row))})
            for row in today:
                errors.append({"station": row["station"], "target_date": target, "model_id": model_id,
                               "label_target_cutoff": model["label_target_cutoff"], "range_band": band(row),
                               "error_f": float(row[f"observed_{metric}_f"] - mean(row, model))})
    return {"models": models, "error_models": error_models, "predictions": predictions,
            "reference_replay_hash": digest(baseline)}


def verify(evidence, identity):
    if (evidence.get("artifact_id") != digest({k: v for k, v in evidence.items() if k != "artifact_id"})
            or evidence.get("identity") != identity
            or any(evidence.get(k) is not False for k in ("historical_as_of_verified", "production_training_eligible", "live_ready", "paper_execution_enabled"))):
        raise ValueError("CONDITIONAL_EVIDENCE_IDENTITY_MISMATCH")
    for group in ("models", "error_models"):
        for key, model in evidence[group].items():
            if digest(model) != key or model["label_target_cutoff"] != cutoff(model["target_date"]):
                raise ValueError("CONDITIONAL_MODEL_IDENTITY_MISMATCH")
    for model in evidence["models"].values():
        if model["latest_training_target"] > model["label_target_cutoff"]:
            raise ValueError("CONDITIONAL_TRAINING_LEAKAGE")
    for model in evidence["error_models"].values():
        for row in model["errors"]:
            prior = evidence["models"].get(row["model_id"])
            if (not prior or prior["target_date"] != row["target_date"] or prior["metric"] != model["metric"]
                    or prior["label_target_cutoff"] != row["label_target_cutoff"]
                    or row["target_date"] > model["label_target_cutoff"]):
                raise ValueError("CONDITIONAL_CALIBRATION_LINK_MISMATCH")
    for p in evidence["predictions"]:
        for group, field in (("models", "model_id"), ("error_models", "error_model_id")):
            m = evidence[group].get(p[field])
            if not m or m["target_date"] != p["target_date"] or m["metric"] != p["metric"]:
                raise ValueError("CONDITIONAL_PREDICTION_LINK_MISMATCH")
        if p["distribution"] != distribution(evidence["error_models"][p["error_model_id"]], p["station"], p["range_band"]):
            raise ValueError("CONDITIONAL_DISTRIBUTION_MISMATCH")


def evaluate(test, evidence):
    lookup = {(p["metric"], p["station"], p["target_date"]): p for p in evidence["predictions"]}
    if len(lookup) != 2 * len(test) or len(lookup) != len(evidence["predictions"]):
        raise ValueError("CONDITIONAL_COHORT_MISMATCH")
    result = {}
    for metric in ("high", "low"):
        scores = {v: [] for v in VARIANTS}
        for row in test:
            p = lookup[metric, row["station"], row["target_date"]]
            observed = row[f"observed_{metric}_f"]
            scores["reference"].append(residual.score_gaussian(observed, p["reference_mean_f"], p["sigma_f"]))
            scores["conditional_mean"].append(residual.score_gaussian(observed, p["mean_f"], p["sigma_f"]))
            d = p["distribution"]
            kernel = residual.mixture_distribution(d["centers_f"], d["weights"], d["kernel_sigma_f"])
            scores["conditional_asymmetric"].append(residual.score_mixture(observed, p["mean_f"], kernel))
        def summary(indices):
            return {v: static.summarize([values[i] for i in indices]) for v, values in scores.items()}
        result[metric] = {"overall": summary(range(len(test))),
            "by_station": {s: summary([i for i, r in enumerate(test) if r["station"] == s]) for s in SPEC["stations"]},
            "by_range_band": {str(b): {"events": sum(band(r) == b for r in test),
                "scores": summary([i for i, r in enumerate(test) if band(r) == b])}
                for b in range(3) if any(band(r) == b for r in test)},
            "paired_vs_reference": static.paired_uncertainty(test, scores[SPEC["primary"][metric]], scores["reference"])}
    return result


def run(history, output):
    source, output = Path(history), Path(output)
    write_once(output / "spec.json", SPEC)
    raw, status_raw = (source / "dataset.json").read_bytes(), (source / "latest.json").read_bytes()
    status = json.loads(status_raw)
    if status.get("status") not in {"complete", "complete_with_gaps"} or status.get("model") != SPEC["model"]:
        raise ValueError("GFS_HISTORY_NOT_COMPLETE")
    identity = {"spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
                "input_sha256": hashlib.sha256(raw).hexdigest(), "status_sha256": hashlib.sha256(status_raw).hexdigest(),
                "numpy_version": np.__version__, "scipy_version": scipy.__version__}
    run_id = digest(identity)
    path = output / f"{run_id}.json"
    if path.exists():
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("identity") != identity or report.get("report_hash") != digest({k: v for k, v in report.items() if k != "report_hash"}):
            raise ValueError("EXISTING_CONDITIONAL_REPORT_MISMATCH")
        artifact = json.loads((output / f"{report['artifact_id']}_replay.json").read_text(encoding="utf-8"))
        verify(artifact, identity)
        if artifact["artifact_id"] != report["artifact_id"]:
            raise ValueError("CONDITIONAL_ARTIFACT_MISMATCH")
        return report
    created = datetime.now(timezone.utc).isoformat()
    rows = static.validate(json.loads(raw), status["dataset_hash"], created)
    rows = [r for r in rows if SPEC["start"] <= r["target_date"] <= SPEC["end"]]
    test = static.split(rows)["test"]
    artifact = {**replay(rows), "identity": identity, "created_at": created,
                "historical_as_of_verified": False, "production_training_eligible": False,
                "live_ready": False, "paper_execution_enabled": False}
    artifact["artifact_id"] = digest(artifact)
    verify(artifact, identity)
    write_once(output / f"{artifact['artifact_id']}_replay.json", artifact)
    report = {"run_id": run_id, "identity": identity, "spec": SPEC, "created_at": created,
              "artifact_id": artifact["artifact_id"], "status": "exploratory_complete_no_promotion",
              "coverage": {"source_station_dates": len(rows), "test_station_dates": len(test),
                           "test_dates": sorted({r["target_date"] for r in test}), "stations": sorted({r["station"] for r in test})},
              "evaluation": evaluate(test, artifact), "deployment_changed": False, "profitability_evaluated": False,
              "limitations": ["Previously inspected test months motivated this hypothesis; repeated comparisons are exploratory.",
                  "Historical forecast availability and three-day label maturity are unverified.",
                  "Forecast temperature range is a proxy, not observed cloud, wind or humidity.",
                  "Rolling earlier test labels may train later dates; current and future labels are excluded.",
                  "Five calendar blocks cannot establish reliable generalization or calibrated coverage.",
                  "Temperature scores do not establish market outperformance, execution quality or profit."]}
    report["report_hash"] = digest(report)
    write_once(path, report)
    return report
