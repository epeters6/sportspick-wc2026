"""Fixed, offline training ablation; never replaces a deployed/frozen model."""
from __future__ import annotations

import hashlib
import json
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sklearn
from scipy.optimize import minimize_scalar
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from backend.ml.weather_learning.dataset import digest, features, implementation_hash as frozen_hash, utc, vector_key
from backend.ml.weather_learning.training import normalize, split_history, weights as original_weights
from backend.weather_model_diagnostics import forward_panel

SPEC = {
    "version": "weather_training_audit_v1",
    "cohort": "saved official labelled v3 vectors with verified input/model timing",
    "features": "unchanged frozen v3 allowlist",
    "split": "unchanged label-embargoed chronological 60/20/20 split",
    "controls": ["original_logistic_refit", "weighted_scaler_only"],
    "station_date_candidate_C": [0.1, 1.0, 10.0],
    "candidate_weight": "one per station/date, equal per vector then bucket within event",
    "temperature_bounds": [0.5, 3.0],
    "selection": "candidate C by calibration station/date log loss only",
    "minimum_calibration_dates": 7,
    "minimum_calibration_station_dates": 75,
    "minimum_calibration_gain_each_score": 0.001,
    "test_usage": "display only; already inspected historical evidence, never a promotion gate",
    "automatic_promotion": False,
}


def implementation_hash():
    # Include the helpers that govern cohort eligibility and time integrity.
    return digest({"audit": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "frozen_v3": frozen_hash(),
                   "diagnostic": hashlib.sha256(Path(__file__).with_name("weather_model_diagnostics.py").read_bytes()).hexdigest()})


def event_key(row):
    return row["metadata"]["station"], row["metadata"]["target_date"]


def groups(rows):
    result = defaultdict(list)
    for i, row in enumerate(rows):
        result[vector_key(row)].append(i)
    return list(result.values())


def event_weights(rows):
    """Fixed event mass, independent of the number of snapshots or buckets."""
    vectors = groups(rows)
    counts = Counter(event_key(rows[idx[0]]) for idx in vectors)
    result = np.empty(len(rows))
    for idx in vectors:
        result[idx] = 1.0 / (counts[event_key(rows[idx[0]])] * len(idx))
    return result


def score(rows, probabilities):
    p = np.asarray(probabilities)
    if len(p) != len(rows) or not np.isfinite(p).all() or np.any((p < 0) | (p > 1)):
        raise ValueError("INVALID_PROBABILITIES")
    events = defaultdict(list)
    for idx in groups(rows):
        if not np.isclose(sum(p[idx]), 1, atol=1e-6, rtol=0):
            raise ValueError("INCOMPLETE_PROBABILITY_VECTOR")
        truth = np.array([int(rows[i]["is_correct"]) for i in idx])
        if sum(truth) != 1:
            raise ValueError("INVALID_LABEL_VECTOR")
        events[event_key(rows[idx[0]])].append((float(np.sum((p[idx] - truth) ** 2)),
                                               float(-np.log(max(1e-12, p[idx] @ truth)))))
    values = np.array([np.mean(v, axis=0) for v in events.values()])
    return {"brier": float(values[:, 0].mean()), "log_loss": float(values[:, 1].mean())}


def fit_logistic(rows, *, kind="station_date", C=0.1):
    if kind not in {"station_date", *SPEC["controls"]}:
        raise ValueError("UNKNOWN_ABLATION")
    vectorizer = DictVectorizer(sparse=False)
    x = vectorizer.fit_transform([features(r) for r in rows])
    w = event_weights(rows) if kind == "station_date" else original_weights(rows)
    scaler = StandardScaler().fit(x, sample_weight=None if kind == "original_logistic_refit" else w)
    estimator = LogisticRegression(C=C, max_iter=1000, random_state=31)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        estimator.fit(scaler.transform(x), [int(r["is_correct"]) for r in rows], sample_weight=w)
    return {"vectorizer": vectorizer, "scaler": scaler, "estimator": estimator,
            "kind": kind, "C": C, "training_weight_sum": float(w.sum())}


def raw_predict(model, rows):
    x = model["vectorizer"].transform([features(r) for r in rows])
    return model["estimator"].predict_proba(model["scaler"].transform(x))[:, 1]


def calibrate(model, rows):
    raw = raw_predict(model, rows)
    temperature = minimize_scalar(lambda t: score(rows, normalize(rows, raw, t))["log_loss"],
                                  bounds=SPEC["temperature_bounds"], method="bounded")
    if not temperature.success:
        raise ValueError("CALIBRATION_FAILED")
    model["temperature"] = float(temperature.x)
    return score(rows, normalize(rows, raw, model["temperature"]))


def select(calibration, rows, market):
    """No test rows/scores accepted by this interface."""
    selected = min(calibration, key=lambda name: calibration[name]["log_loss"])
    enough = (len({event_key(r) for r in rows}) >= SPEC["minimum_calibration_station_dates"]
              and len({r["metadata"]["target_date"] for r in rows}) >= SPEC["minimum_calibration_dates"])
    gain = all(market[k] - calibration[selected][k] >= SPEC["minimum_calibration_gain_each_score"]
               for k in ("brier", "log_loss"))
    return {"candidate": selected, "calibration_gate_passed": enough and gain,
            "reason": "insufficient_calibration_coverage" if not enough else "qualified_calibration_gain" if gain else "no_qualified_calibration_gain",
            "deployment": "none_research_only", "used_test_outcomes": False}


def evaluate(raw, as_of):
    unique = {}
    for row in raw:
        identity = row.get("id")
        if not identity:
            raise ValueError("MISSING_ROW_IDENTITY")
        if identity in unique and unique[identity] != row:
            raise ValueError("CONFLICTING_ROW_IDENTITY")
        unique[identity] = row
    panel, quality = forward_panel(list(unique.values()), as_of)
    rows = [r for v in panel for r in v["rows"]]
    train, cal, test = split_history(rows)
    market = {label: score(part, [r["metadata"]["market_vector_prob"] for r in part])
              for label, part in (("calibration", cal), ("test", test))}
    fits, evaluations, calibration = {}, {}, {}
    variants = [(name, name, .1) for name in SPEC["controls"]]
    variants += [(f"station_date_C_{C:g}", "station_date", C) for C in SPEC["station_date_candidate_C"]]
    for name, kind, C in variants:
        model = fit_logistic(train, kind=kind, C=C)
        values = calibrate(model, cal)
        fits[name] = model
        evaluations[name] = {"calibration": values, "temperature": model["temperature"],
                             "training_weight_sum": model["training_weight_sum"]}
        if kind == "station_date":
            calibration[name] = values
    selection = select(calibration, cal, market["calibration"])
    # Model choice is complete before any candidate's test predictions are scored.
    for name, model in fits.items():
        p = normalize(test, raw_predict(model, test), model["temperature"])
        evaluations[name]["test"] = score(test, p)
    saved = {name: {"test": score(test, [r["metadata"]["challenger_probabilities"][name] for r in test])}
             for name in ("regularized_logistic", "boosted_trees")}
    split = {name: {"rows": len(part), "vectors": len(groups(part)),
                    "station_dates": len({event_key(r) for r in part}),
                    "dates": sorted({r["metadata"]["target_date"] for r in part}),
                    "first_decision_at": min(utc(r["metadata"]["decision_at"]) for r in part).isoformat(),
                    "last_label_at": max(utc(r["resolved_at"]) for r in part).isoformat(),
                    "row_ids_hash": digest(sorted(r["id"] for r in part))}
             for name, part in (("train", train), ("calibration", cal), ("test", test))}
    selected = fits[selection["candidate"]]
    parameters = {"feature_names": list(selected["vectorizer"].get_feature_names_out()),
                  "mean": selected["scaler"].mean_.tolist(), "scale": selected["scaler"].scale_.tolist(),
                  "coefficients": selected["estimator"].coef_.tolist(),
                  "intercept": selected["estimator"].intercept_.tolist(),
                  "C": selected["C"], "temperature": selected["temperature"]}
    return {"quality": quality, "duplicate_identical_rows_removed": len(raw) - len(unique),
            "split": split, "selection": selection, "market_baseline": market,
            "ablations": evaluations, "saved_forward_models_same_test_cohort": saved,
            "research_candidate_parameters": parameters,
            "live_ready": False, "paper_execution_enabled": False,
            "limitations": ["Exploratory retrospective refits on previously inspected history; not new forward evidence.",
                            "All variants use the same enriched v3 cohort; controls do not reproduce the deployed mixed v2/v3 training set.",
                            "All ablations use station/date-weighted calibration and evaluation, including controls.",
                            "Station/date weighting does not make cities on the same date independent.",
                            "Binary bucket fitting followed by normalization remains; this trial does not fix its objective mismatch.",
                            "No trade-cost, fill, profit, or live-readiness conclusion; no model promotion."]}


def run(source, output, *, as_of=None):
    source, output = Path(source), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).isoformat()
    cutoff = utc(as_of or created).isoformat()
    content = source.read_bytes()
    identity = {"spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
                "input_sha256": hashlib.sha256(content).hexdigest(), "evidence_as_of": cutoff,
                "sklearn_version": sklearn.__version__}
    run_id = digest(identity)
    path = output / f"{run_id}.json"
    if path.exists():
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("identity") != identity or report.get("report_hash") != digest({k: v for k, v in report.items() if k != "report_hash"}):
            raise ValueError("EXISTING_REPORT_MISMATCH")
        return report
    # Save the exact specification before fitting/scoring any real data.
    spec_path = output / f"{identity['spec_hash']}_spec.json"
    if spec_path.exists():
        if json.loads(spec_path.read_text(encoding="utf-8")) != SPEC:
            raise ValueError("EXISTING_SPEC_MISMATCH")
    else:
        with spec_path.open("x", encoding="utf-8") as f:
            json.dump(SPEC, f, indent=2, allow_nan=False)
    raw = json.loads(content)
    if isinstance(raw, dict):
        raw = raw["v3"]
    report = {"run_id": run_id, "identity": identity, "created_at": created,
              "spec": SPEC, "mode": "offline_training_audit", **evaluate(raw, cutoff)}
    report["report_hash"] = digest(report)
    with path.open("x", encoding="utf-8") as f:
        json.dump(report, f, indent=2, allow_nan=False)
    return report
