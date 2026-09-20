"""Small-data baselines and boosted trees with chronological, grouped evaluation."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import sklearn
from scipy.optimize import minimize_scalar
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import VERSION
from .dataset import digest, event_key, features, finite, implementation_hash, prepare, utc, vector_key


def weights(rows):
    counts = Counter(event_key(r) for r in rows)
    # Repeated hourly snapshots and mutually exclusive buckets carry one total
    # station-day weight, including observations of that day on another venue.
    return np.array([1 / counts[event_key(r)] for r in rows]) * len(rows) / len(counts)


def normalize(rows, probabilities, temperature=1.0):
    values = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1 - 1e-6)
    result = np.empty(len(rows))
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        groups[vector_key(row)].append(i)
    for indices in groups.values():
        logp = np.log(values[indices]) / temperature
        p = np.exp(logp - max(logp))
        result[indices] = p / sum(p)
    return result


def scores(rows, probabilities):
    p = np.asarray(probabilities)
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        groups[vector_key(row)].append(i)
    per_event = defaultdict(list)
    for indices in groups.values():
        truth = np.array([int(rows[i]["is_correct"]) for i in indices])
        pred = p[indices]
        per_event[event_key(rows[indices[0]])].append({
            "brier": float(np.sum((pred - truth) ** 2)),
            "log_loss": float(-np.log(max(1e-6, pred[np.argmax(truth)]))),
        })
    result = {name: float(np.mean([np.mean([s[name] for s in snapshots]) for snapshots in per_event.values()]))
              for name in ("brier", "log_loss")}
    result["station_days"] = len(per_event)
    result["dates"] = len({r["metadata"]["target_date"] for r in rows})
    # Selected-price diagnostic is not reported as executed/realized profit.
    # One first qualifying bucket per station-day, with the original all-in cost.
    chosen = {}
    for i in sorted(range(len(rows)), key=lambda j: rows[j]["metadata"].get("decision_at", rows[j]["created_at"])):
        row, probability = rows[i], p[i]
        key = event_key(row)
        cost = finite(row["metadata"].get("executable_cost"))
        if key not in chosen and cost is not None and 0 < cost < 1 and probability - cost >= 0.03:
            chosen[key] = (int(row["is_correct"]) - cost, cost)
    result["price_only_diagnostic"] = {
        "events": len(chosen), "unit_pnl": sum(x[0] for x in chosen.values()),
        "unit_cost": sum(x[1] for x in chosen.values()), "fills_verified": False,
        "description": "First qualifying one-contract quote per station-day; no claim of executable profit.",
    }
    bins = []
    w = weights(rows)
    for low, high in ((0, .05), (.05, .1), (.1, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
        indices = [i for i, row in enumerate(rows) if low <= p[i] < high]
        if indices:
            bins.append({"range": [low, min(high, 1)], "rows": len(indices),
                         "predicted": float(np.average(p[indices], weights=w[indices])),
                         "observed": float(np.average([int(rows[i]["is_correct"]) for i in indices], weights=w[indices]))})
    result["reliability"] = bins
    for direction in ("lower", "middle", "upper"):
        indices = [i for i, r in enumerate(rows) if (
            "lower" if r["metadata"].get("bucket_low_f") is None else
            "upper" if r["metadata"].get("bucket_high_f") is None else "middle") == direction]
        if indices:
            result[direction] = {"predicted": float(np.average(p[indices], weights=w[indices])),
                                 "observed": float(np.average([int(rows[i]["is_correct"]) for i in indices], weights=w[indices]))}
    return result


def split_history(rows):
    dates = sorted({r["metadata"]["target_date"] for r in rows})
    if len(dates) < 6:
        raise ValueError("NEED_SIX_DISTINCT_DATES_FOR_TRAIN_CALIBRATION_TEST")
    n_test = max(1, len(dates) // 5)
    n_cal = max(1, len(dates) // 5)
    test_rows = [r for r in rows if r["metadata"]["target_date"] in dates[-n_test:]]
    grouped = defaultdict(list)
    for row in rows:
        grouped[vector_key(row)].append(row)

    def available_before(next_rows):
        cutoff = min(utc(r["metadata"].get("decision_at") or r["created_at"]) for r in next_rows)
        first_day = min(r["metadata"]["target_date"] for r in next_rows)
        return [r for group in grouped.values()
                if group[0]["metadata"]["target_date"] < first_day
                and all(utc(r["resolved_at"]) < cutoff for r in group) for r in group]

    # Leave a data-dependent embargo for next-day forecasts and delayed labels.
    # Which dates qualify depends only on timestamps, never model performance.
    cal_pool = available_before(test_rows)
    cal_days = sorted({r["metadata"]["target_date"] for r in cal_pool})[-n_cal:]
    cal_rows = [r for r in cal_pool if r["metadata"]["target_date"] in cal_days]
    partitions = [available_before(cal_rows) if cal_rows else [], cal_rows, test_rows]
    if any(not part for part in partitions):
        raise ValueError("INSUFFICIENT_LABELS_AVAILABLE_AT_SPLIT_CUTOFF")
    return partitions


def train(rows: list[dict], output: Path, *, as_of=None):
    cutoff = utc(as_of or datetime.now(timezone.utc))
    rows, quality = prepare(rows, cutoff)
    report = {"version": VERSION, "trained_at": cutoff.isoformat(), "mode": "shadow",
              "implementation_hash": implementation_hash(),
              "live_ready": False, "paper_execution_enabled": False, "quality": quality,
              "minimum_forward_dates": 45, "minimum_forward_station_days": 250,
              "limitations": ["Chronological research holdout; rolling retraining is not a permanent unseen test.",
                              "Forecast accuracy and quote diagnostics do not establish executable trading profit.",
                              "Existing v2 ledgers and risk stops are unchanged."]}
    try:
        train_rows, cal_rows, test_rows = split_history(rows)
    except ValueError as exc:
        report.update(status="collecting", reason=str(exc))
        write_report(report, output)
        return report
    vectorizer = DictVectorizer(sparse=False)
    x_train = vectorizer.fit_transform([features(r) for r in train_rows])
    x_cal = vectorizer.transform([features(r) for r in cal_rows])
    x_test = vectorizer.transform([features(r) for r in test_rows])
    y = np.array([int(r["is_correct"]) for r in train_rows])
    candidates = {
        "regularized_logistic": make_pipeline(StandardScaler(), LogisticRegression(C=.1, max_iter=1000, random_state=31)),
        "boosted_trees": HistGradientBoostingClassifier(max_iter=100, learning_rate=.05, max_leaf_nodes=7,
            min_samples_leaf=30, l2_regularization=5.0, early_stopping=False, random_state=31),
    }
    models, evaluation = {}, {}
    for name, estimator in candidates.items():
        if name == "regularized_logistic":
            estimator.fit(x_train, y, logisticregression__sample_weight=weights(train_rows))
        else:
            estimator.fit(x_train, y, sample_weight=weights(train_rows))
        raw_cal = estimator.predict_proba(x_cal)[:, 1]
        temp = float(minimize_scalar(lambda t: scores(cal_rows, normalize(cal_rows, raw_cal, t))["log_loss"],
                                    bounds=(.5, 3.0), method="bounded").x)
        models[name] = {"estimator": estimator, "temperature": temp}
        evaluation[name] = {
            "calibration": scores(cal_rows, normalize(cal_rows, raw_cal, temp)),
            "test": scores(test_rows, normalize(test_rows, estimator.predict_proba(x_test)[:, 1], temp)),
            "temperature": temp,
        }
    market = normalize(test_rows, [r["metadata"]["market_vector_prob"] for r in test_rows])
    raw = normalize(test_rows, [finite(r["metadata"].get("raw_model_prob"), r["prob"]) for r in test_rows])
    current = normalize(test_rows, [r["prob"] for r in test_rows])
    evaluation["market_baseline"] = {"test": scores(test_rows, market)}
    evaluation["original_forecast"] = {"test": scores(test_rows, raw)}
    evaluation["current_probability"] = {"test": scores(test_rows, current)}
    # Choose only on calibration dates; never select on the displayed test scores.
    selected = min(models, key=lambda name: evaluation[name]["calibration"]["log_loss"])
    report.update(status="trained_research_only", selected_model=selected, evaluation=evaluation,
                  split={name: {"rows": len(part), "station_days": len({event_key(r) for r in part}),
                                "dates": sorted({r["metadata"]["target_date"] for r in part}),
                                "last_label_at": max(r["resolved_at"] for r in part)}
                         for name, part in zip(("train", "calibration", "test"), (train_rows, cal_rows, test_rows))},
                  sklearn_version=sklearn.__version__,
                  enriched_rows=sum(bool(r["metadata"].get("weather_features")) for r in train_rows))
    report["model_id"] = digest({"version": VERSION, "data": quality["dataset_hash"], "cutoff": cutoff.isoformat(), "selected": selected})
    output.mkdir(parents=True, exist_ok=True)
    path = output / "model.joblib"
    artifact = {"version": VERSION, "implementation_hash": report["implementation_hash"], "model_id": report["model_id"], "trained_at": cutoff.isoformat(),
                "vectorizer": vectorizer, "models": models, "selected": selected, "sklearn_version": sklearn.__version__}
    joblib.dump(artifact, path)
    report["artifact_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_report(report, output)
    return report


def write_report(report, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "training.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# Weather learning research", "", f"Status: **{report['status']}**. Shadow only; no orders.", "",
             f"Verified data: {report['quality']['station_days']} station-days across {report['quality']['distinct_dates']} dates.", "",
             "Model | Held-out Brier (lower is better) | Held-out log loss (lower is better)", "--- | ---: | ---:"]
    for name, values in report.get("evaluation", {}).items():
        lines.append(f"{name} | {values['test']['brier']:.4f} | {values['test']['log_loss']:.4f}")
    lines += ["", "These scores do not establish profitability. Quote diagnostics do not verify fills."]
    (output / "training.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_artifact(directory: Path):
    report = json.loads((directory / "training.json").read_text(encoding="utf-8"))
    path = directory / "model.joblib"
    if report.get("status") != "trained_research_only" or report.get("version") != VERSION:
        return None
    if report.get("implementation_hash") != implementation_hash():
        raise ValueError("MODEL_IMPLEMENTATION_MISMATCH")
    if report.get("sklearn_version") != sklearn.__version__:
        raise ValueError("MODEL_ARTIFACT_VERSION_MISMATCH")
    if hashlib.sha256(path.read_bytes()).hexdigest() != report.get("artifact_sha256"):
        raise ValueError("MODEL_ARTIFACT_HASH_MISMATCH")
    # Only artifacts produced by this repo's trusted training job are loaded.
    model = joblib.load(path)
    if model["model_id"] != report["model_id"] or model["sklearn_version"] != sklearn.__version__:
        raise ValueError("MODEL_ARTIFACT_VERSION_MISMATCH")
    return model


def predict(rows, artifact):
    if not artifact:
        return {}
    if any(utc(r["metadata"].get("decision_at") or r["created_at"]) < utc(artifact["trained_at"]) for r in rows):
        raise ValueError("MODEL_TRAINED_AFTER_DECISION")
    matrix = artifact["vectorizer"].transform([features(r) for r in rows])
    return {name: normalize(rows, item["estimator"].predict_proba(matrix)[:, 1], item["temperature"]).tolist()
            for name, item in artifact["models"].items()}
