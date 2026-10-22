"""Separate retrospective residual recentering and asymmetric error distributions.

Uses only cached evidence. Historical publication timing is unverified and the
three-day label maturity lag is an assumption, not a forward-eligibility claim.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import scipy
from scipy.optimize import brentq
from scipy.special import logsumexp, ndtr, ndtri

from backend.ml.weather_learning.dataset import digest
from backend.weather_probability_screen import write_once
from backend import weather_adaptive_calibration as parent
from backend import weather_station_distribution as static

VARIANTS = ("raw_gfs", "pr19", "residual_recenter_only", "residual_gaussian", "residual_kernel")
SPEC = {
    "version": "weather_residual_calibration_v1", "model": "gfs_global",
    "start": "2026-04-03", "end": "2026-10-18",
    "test_start": "2026-09-20", "test_end": "2026-10-18",
    "stations": list(parent.SPEC["stations"]),
    "assumed_label_lag_target_days": 3,
    "residual_window_calendar_days": 30, "minimum_dates_per_station": 21,
    "pooled_prior_station_days": 15.0, "kernel_sigma_f": 1.0,
    "parent_spec_hash": digest(parent.SPEC),
    "residual_definition": "Observed minus PR19 rolling mean, whose own target label was excluded",
    "distribution": "Station/pool weighted Gaussian kernels at prior signed residuals; no recentering away residual bias",
    "point_forecast": "Distribution mean for every variant",
    "variants": list(VARIANTS), "primary_candidate": "residual_kernel",
    "interval_coverages": list(parent.SPEC["interval_coverages"]),
    "selection": "All variants fixed and reported, no tuning, station selection or promotion",
    "evaluation_type": "Exploratory rolling replay on previously inspected months",
    "historical_as_of_verified": False,
}


def implementation_hash():
    return digest({"source": Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n"),
                   "parent_and_helpers": parent.implementation_hash()})


def mixture_weights(residuals, station):
    """One weight per unique station/date, with a fixed pooled prior."""
    own = np.array([r["station"] == station for r in residuals], dtype=float)
    count = own.sum()
    if count < SPEC["minimum_dates_per_station"]:
        raise ValueError("INSUFFICIENT_STATION_RESIDUALS")
    prior = SPEC["pooled_prior_station_days"]
    return (own + prior / len(residuals)) / (count + prior)


def residual_model(residuals, target, metric, stations):
    cutoff = (date.fromisoformat(target) - timedelta(days=SPEC["assumed_label_lag_target_days"])).isoformat()
    selected = parent.window(residuals, cutoff, SPEC["residual_window_calendar_days"])
    if len({(r["station"], r["target_date"]) for r in selected}) != len(selected):
        raise ValueError("DUPLICATE_RESIDUAL_EVENT")
    for r in selected:
        if r["label_target_cutoff"] > (date.fromisoformat(r["target_date"]) - timedelta(days=3)).isoformat():
            raise ValueError("RESIDUAL_TARGET_LEAKAGE")
    errors = np.array([r["error_f"] for r in selected])
    if not np.isfinite(errors).all():
        raise ValueError("INVALID_RESIDUAL")
    fitted = {}
    for station in stations:
        weights = mixture_weights(selected, station)
        shift = float(weights @ errors)
        sigma = math.sqrt(float(weights @ (errors - shift) ** 2) + SPEC["kernel_sigma_f"] ** 2)
        fitted[station] = {"mean_shift_f": shift, "moment_sigma_f": sigma,
                          "residual_dates": sum(r["station"] == station for r in selected)}
    return {"target_date": target, "metric": metric, "label_target_cutoff": cutoff,
            "residuals": selected, "stations": fitted}


def replay(rows):
    original = parent.replay(rows)
    by_day = {(m["metric"], m["target_date"]): (key, m) for key, m in original["models"].items()}
    residuals = {metric: [] for metric in ("high", "low")}
    for row in rows:
        for metric in residuals:
            item = by_day.get((metric, row["target_date"]))
            if item is None:
                continue
            model_id, model = item
            mu = row[f"forecast_{metric}_f"] + model["stations"][row["station"]]["bias_f"]
            residuals[metric].append({"station": row["station"], "target_date": row["target_date"],
                                      "parent_model_id": model_id, "label_target_cutoff": model["label_target_cutoff"],
                                      "error_f": row[f"observed_{metric}_f"] - mu})
    models, lookup, predictions = {}, {}, []
    stations = sorted({r["station"] for r in rows})
    for p in original["predictions"]:
        key = (p["metric"], p["target_date"])
        if key not in lookup:
            model = residual_model(residuals[p["metric"]], p["target_date"], p["metric"], stations)
            model_id = digest(model)
            models[model_id] = model
            lookup[key] = model_id
        model_id = lookup[key]
        local = models[model_id]["stations"][p["station"]]
        predictions.append({**p, "parent_model_id": p["model_id"], "model_id": model_id,
                            "parent_mean_f": p["mean_f"], "parent_sigma_f": p["sigma_f"],
                            "mean_f": p["mean_f"] + local["mean_shift_f"], "sigma_f": local["moment_sigma_f"]})
    return {"parent_models": original["models"], "models": models, "predictions": predictions}


def expected_absolute_normal(delta, sigma):
    z = np.asarray(delta) / sigma
    return 2 * sigma * np.exp(-.5 * z ** 2) / math.sqrt(2 * math.pi) + delta * (2 * ndtr(z) - 1)


def mixture_distribution(errors, weights, sigma):
    """Parameters and proper-score normalizer; no outcome is accepted here."""
    errors, weights = np.asarray(errors, dtype=float), np.asarray(weights, dtype=float)
    if (errors.ndim != 1 or not len(errors) or weights.shape != errors.shape
            or not np.isfinite(errors).all() or not np.isfinite(weights).all()
            or np.any(weights <= 0) or not np.isclose(weights.sum(), 1, atol=1e-12, rtol=0)
            or not math.isfinite(sigma) or sigma <= 0):
        raise ValueError("INVALID_MIXTURE")
    # CRPS = E|X-y| - 1/2 E|X-X'|. Shared Gaussian component width.
    pair = expected_absolute_normal(errors[:, None] - errors[None, :], math.sqrt(2) * sigma)
    correction = float(.5 * weights @ pair @ weights)
    intervals = {}
    for coverage in SPEC["interval_coverages"]:
        bounds = []
        for quantile in ((1 - coverage) / 2, (1 + coverage) / 2):
            bounds.append(float(brentq(lambda x: float(weights @ ndtr((x - errors) / sigma)) - quantile,
                                       float(errors.min() - 12 * sigma), float(errors.max() + 12 * sigma), xtol=1e-10)))
        intervals[str(round(100 * coverage))] = bounds
    return {"errors": errors, "weights": weights, "sigma": sigma, "crps_correction": correction,
            "intervals": intervals, "mean": float(weights @ errors)}


def interval_scores(observed, bounds):
    result = {}
    for name, (lo, hi) in bounds.items():
        alpha = 1 - int(name) / 100
        width = hi - lo
        result.update({f"coverage_{name}": float(lo <= observed <= hi), f"width_{name}_f": width,
                       f"miss_below_{name}": float(observed < lo), f"miss_above_{name}": float(observed > hi),
                       f"interval_score_{name}_f": width + 2 / alpha * max(lo - observed, 0, observed - hi)})
    return result


def score_mixture(observed, base_mean, distribution):
    errors, weights, sigma = distribution["errors"], distribution["weights"], distribution["sigma"]
    residual = observed - base_mean
    mean_error = observed - base_mean - distribution["mean"]
    crps = float(weights @ expected_absolute_normal(residual - errors, sigma)) - distribution["crps_correction"]
    nll = -float(logsumexp(np.log(weights) - .5 * ((residual - errors) / sigma) ** 2 - math.log(sigma * math.sqrt(2 * math.pi))))
    return {"mae_f": abs(mean_error), "squared_error_f2": mean_error ** 2, "forecast_bias_f": -mean_error,
            "crps_f": crps, "nll": nll,
            **interval_scores(residual, distribution["intervals"])}


def score_gaussian(observed, mu, sigma):
    values = static.observation_scores(observed, mu, sigma)
    values["nll"] = values.pop("gaussian_nll")
    bounds = {str(round(c * 100)): (mu - float(ndtri((1+c)/2)) * sigma, mu + float(ndtri((1+c)/2)) * sigma)
              for c in SPEC["interval_coverages"]}
    values.update(interval_scores(observed, bounds))
    return values


def evaluate(test, evidence, raw_models):
    lookup = {(p["metric"], p["station"], p["target_date"]): p for p in evidence["predictions"]}
    if len(lookup) != 2 * len(test) or len(lookup) != len(evidence["predictions"]):
        raise ValueError("PREDICTION_COHORT_MISMATCH")
    result = {}
    for metric in ("high", "low"):
        scores = {v: [] for v in VARIANTS}
        for row in test:
            p = lookup[metric, row["station"], row["target_date"]]
            model = evidence["models"][p["model_id"]]
            raw = static.distribution(row[f"forecast_{metric}_f"], row["station"], raw_models[metric], "raw_gfs")
            observed = row[f"observed_{metric}_f"]
            for variant, params in {"raw_gfs": raw, "pr19": (p["parent_mean_f"], p["parent_sigma_f"]),
                                    "residual_recenter_only": (p["mean_f"], p["parent_sigma_f"]),
                                    "residual_gaussian": (p["mean_f"], p["sigma_f"])}.items():
                scores[variant].append(score_gaussian(observed, *params))
            kernel = mixture_distribution([r["error_f"] for r in model["residuals"]],
                                          mixture_weights(model["residuals"], row["station"]), SPEC["kernel_sigma_f"])
            scores["residual_kernel"].append(score_mixture(observed, p["parent_mean_f"], kernel))
        result[metric] = {
            "overall": {v: static.summarize(s) for v, s in scores.items()},
            "by_station": {station: {v: static.summarize([s for r, s in zip(test, values) if r["station"] == station])
                                     for v, values in scores.items()} for station in SPEC["stations"]},
            "paired_vs_pr19": static.paired_uncertainty(test, scores["residual_kernel"], scores["pr19"]),
            "paired_shape_only": static.paired_uncertainty(test, scores["residual_kernel"], scores["residual_gaussian"]),
        }
    return result


def verify_evidence(evidence, identity):
    if (evidence.get("artifact_id") != digest({k: v for k, v in evidence.items() if k != "artifact_id"})
            or evidence.get("identity") != identity
            or any(evidence.get(k) is not False for k in ("historical_as_of_verified", "production_training_eligible", "live_ready", "paper_execution_enabled"))):
        raise ValueError("RESIDUAL_EVIDENCE_IDENTITY_MISMATCH")
    for group in ("models", "parent_models"):
        if any(digest(m) != key for key, m in evidence[group].items()):
            raise ValueError("RESIDUAL_MODEL_IDENTITY_MISMATCH")
    for model in evidence["models"].values():
        for r in model["residuals"]:
            pm = evidence["parent_models"].get(r["parent_model_id"])
            if (not pm or pm["target_date"] != r["target_date"] or pm["metric"] != model["metric"]
                    or pm["label_target_cutoff"] != r["label_target_cutoff"] or r["target_date"] > model["label_target_cutoff"]):
                raise ValueError("RESIDUAL_PARENT_LINK_MISMATCH")
    for p in evidence["predictions"]:
        model, pm = evidence["models"].get(p["model_id"]), evidence["parent_models"].get(p["parent_model_id"])
        if any(not m or m["target_date"] != p["target_date"] or m["metric"] != p["metric"] for m in (model, pm)):
            raise ValueError("RESIDUAL_PREDICTION_LINK_MISMATCH")


def run(history_directory, output):
    source, output = Path(history_directory), Path(output)
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
            raise ValueError("EXISTING_RESIDUAL_REPORT_MISMATCH")
        artifact = json.loads((output / f"{report['artifact_id']}_replay.json").read_text(encoding="utf-8"))
        verify_evidence(artifact, identity)
        if artifact["artifact_id"] != report["artifact_id"]:
            raise ValueError("RESIDUAL_EVIDENCE_IDENTITY_MISMATCH")
        return report
    created = datetime.now(timezone.utc).isoformat()
    rows = static.validate(json.loads(raw), status["dataset_hash"], created)
    rows = [r for r in rows if SPEC["start"] <= r["target_date"] <= SPEC["end"]]
    parts = static.split(rows)
    raw_models = {m: static.fit_metric(parts["train"], parts["calibration"], m) for m in ("high", "low")}
    artifact = {**replay(rows), "identity": identity, "created_at": created, "raw_models": raw_models,
                "mode": "retrospective_residual_replay", "historical_as_of_verified": False,
                "production_training_eligible": False, "live_ready": False, "paper_execution_enabled": False}
    artifact["artifact_id"] = digest(artifact)
    verify_evidence(artifact, identity)
    write_once(output / f"{artifact['artifact_id']}_replay.json", artifact)
    test = parts["test"]
    expected = (date.fromisoformat(SPEC["end"]) - date.fromisoformat(SPEC["start"])).days + 1
    report = {"run_id": run_id, "identity": identity, "spec": SPEC, "created_at": created,
              "artifact_id": artifact["artifact_id"], "status": "exploratory_residual_comparison_complete_no_promotion",
              "coverage": {"source_station_dates": len(rows), "source_missing_station_dates": expected * len(SPEC["stations"]) - len(rows),
                           "test_station_dates": len(test), "test_dates": sorted({r["target_date"] for r in test}),
                           "test_by_station": {s: sum(r["station"] == s for r in test) for s in SPEC["stations"]}},
              "evaluation": evaluate(test, artifact, raw_models),
              "earlier_evaluation_labels_used_for_later_fits": True, "parameters_tuned_on_evaluation": False,
              "current_or_future_target_labels_used": False, "deployment_changed": False, "profitability_evaluated": False,
              "limitations": ["Previously inspected months and repeated research comparisons; not an unseen holdout.",
                              "Label maturity and historical forecast availability remain unverified; no genuinely forward evidence.",
                              "Kernel shape and fixed windows can miss changing error regimes; no coverage guarantee.",
                              "Only one GFS run per target; the full multi-model decision-hour archive is separate.",
                              "Paired seven-day intervals have five calendar blocks and do not account for model-selection/fitting uncertainty.",
                              "Temperature and distribution scores do not demonstrate market outperformance or executable profit."]}
    report["report_hash"] = digest(report)
    write_once(path, report)
    return report
