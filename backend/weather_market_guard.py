"""Separate market-anchored shadow policy. Never replace frozen v3 forecasts."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import numpy as np

from backend.ml.weather_learning.dataset import digest, implementation_hash as learning_hash, utc
from backend.weather_model_diagnostics import MODELS, event_key, forward_panel
from backend.weather_probability_screen import write_once

SPEC = {"version": "weather_market_guard_v1", "calibration_dates": 10, "minimum_dates": 7,
        "minimum_station_dates": 75, "weights": [0.0, 0.1, 0.25, 0.5],
        "maximum_probability_change": 0.05, "minimum_gain": 0.001,
        "minimum_fraction_dates_improved": 0.6,
        "selection": "Earlier officially labelled saved forward forecasts only; both proper scores must improve.",
        "scope": "all_stations_high_low_venues_share_station_date_weight"}


def implementation_hash():
    paths = [Path(__file__), Path(__file__).with_name("weather_model_diagnostics.py")]
    return digest({"learning": learning_hash(), **{p.name: p.read_text(encoding="utf-8").replace("\r\n", "\n") for p in paths}})


def blend(market, prediction, weight):
    market = np.asarray(market, dtype=float)
    if weight == 0:
        return market.copy()
    delta = weight * (np.asarray(prediction, dtype=float) - market)
    largest = float(np.abs(delta).max())
    if largest > SPEC["maximum_probability_change"]:
        delta *= SPEC["maximum_probability_change"] / largest
    return market + delta


def score(panel, name=None, weight=0.0):
    events = defaultdict(list)
    for v in panel:
        p = blend(v["market"], v["predictions"].get(name, v["market"]), weight)
        truth = np.array([int(r["is_correct"]) for r in v["rows"]])
        events[event_key(v)].append({"brier": float(np.sum((p-truth)**2)),
                                     "log_loss": float(-np.log(max(1e-12, p @ truth)))})
    return {k: mean(mean(s[k] for s in values) for values in events.values()) for k in ("brier", "log_loss")} if events else None


def fit(raw, *, as_of=None):
    cutoff = utc(as_of or datetime.now(timezone.utc))
    panel, quality = forward_panel(raw, cutoff)
    dates = sorted({v["target_date"] for v in panel})[-SPEC["calibration_dates"]:]
    calibration = [v for v in panel if v["target_date"] in dates]
    enough = len(dates) >= SPEC["minimum_dates"] and len({event_key(v) for v in calibration}) >= SPEC["minimum_station_dates"]
    baseline = score(calibration)
    candidates = []
    if enough:
        by_date = {day: [v for v in calibration if v["target_date"] == day] for day in dates}
        for name in MODELS:
            for weight in SPEC["weights"]:
                if not weight:
                    continue
                scores = score(calibration, name, weight)
                fraction = mean(all(score(part, name, weight)[k] < score(part)[k] for k in ("brier", "log_loss"))
                                for part in by_date.values())
                candidates.append({"name": name, "weight": weight, "scores": scores, "fraction_dates_improved": fraction})
    eligible = [c for c in candidates if c["fraction_dates_improved"] >= SPEC["minimum_fraction_dates_improved"]
                and all(c["scores"][k] < baseline[k] - SPEC["minimum_gain"] for k in ("brier", "log_loss"))]
    chosen = min(eligible, key=lambda c: (c["scores"]["log_loss"], c["weight"], c["name"])) if eligible else {"name": "market", "weight": 0.0}
    model = {"version": SPEC["version"], "spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
             "created_at": datetime.now(timezone.utc).isoformat(), "calibration_cutoff": cutoff.isoformat(),
             "last_label_at": max((r["resolved_at"] for v in calibration for r in v["rows"]), key=utc, default=None),
             "calibration_dates": dates, "calibration_station_dates": len({event_key(v) for v in calibration}),
             "calibration_hash": digest(calibration), "selected": chosen["name"], "weight": chosen["weight"],
             "market_scores": baseline, "candidates": candidates, "quality": quality,
             "reason": "calibration_gain" if eligible else ("insufficient_calibration" if not enough else "no_qualified_gain"),
             "mode": "shadow_only", "live_ready": False, "paper_execution_enabled": False,
             "independent_test": False, "profitability_proven": False}
    model["model_id"] = digest(model)
    return model


def verify(model):
    if (model.get("spec_hash") != digest(SPEC) or model.get("implementation_hash") != implementation_hash()
            or digest({k:v for k,v in model.items() if k != "model_id"}) != model.get("model_id")):
        raise ValueError("GUARD_MODEL_IDENTITY_MISMATCH")
    if model["weight"] not in SPEC["weights"] or model["selected"] not in ("market", *MODELS):
        raise ValueError("INVALID_GUARD_SELECTION")
    if (model["selected"] == "market") != (model["weight"] == 0):
        raise ValueError("INVALID_GUARD_FALLBACK")
    if utc(model["calibration_cutoff"]) > utc(model["created_at"]):
        raise ValueError("GUARD_FUTURE_CALIBRATION")


def save_model(model, output):
    verify(model)
    output = Path(output)
    write_once(output / "spec.json", SPEC)
    write_once(output / "models" / (model["model_id"] + ".json"), model)
    # The latest pointer is replaceable; model evidence is immutable.
    temporary = output / "latest_model.tmp"
    temporary.write_text(json.dumps({"model_id": model["model_id"]}), encoding="utf-8")
    temporary.replace(output / "latest_model.json")


def load_model(output):
    output = Path(output)
    pointer = json.loads((output / "latest_model.json").read_text(encoding="utf-8"))
    identity = pointer["model_id"]
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("INVALID_GUARD_MODEL_PATH")
    model = json.loads((output / "models" / (identity + ".json")).read_text(encoding="utf-8"))
    verify(model)
    if model["model_id"] != identity:
        raise ValueError("GUARD_POINTER_MISMATCH")
    return model


def predict_forward(raw, model, output, *, as_of=None):
    verify(model)
    now = utc(as_of or datetime.now(timezone.utc))
    panel, quality = forward_panel(raw, now, labelled=False)
    forecasts = []
    for v in panel:
        decision = utc(v["decision_at"])
        if utc(model["created_at"]) >= decision or (model["last_label_at"] and utc(model["last_label_at"]) >= decision):
            raise ValueError("GUARD_NOT_AVAILABLE_BEFORE_DECISION")
        if any(r.get("resolved_at") is not None or r.get("is_correct") is not None for r in v["rows"]):
            raise ValueError("GUARD_REQUIRES_UNLABELLED_DECISIONS")
        if any(now >= utc(r["metadata"]["contract"]["window_end"]) for r in v["rows"]):
            raise ValueError("GUARD_RECORDED_AFTER_OBSERVATION_WINDOW")
        p = blend(v["market"], v["predictions"].get(model["selected"], v["market"]), model["weight"])
        evidence = {"source": SPEC["version"], "guard_model_id": model["model_id"], "guard_created_at": model["created_at"],
                    "base_model_id": v["model_id"], "base_model_trained_at": v["model_trained_at"],
                    "decision_at": v["decision_at"], "recorded_at": now.isoformat(), "station": v["station"],
                    "target_date": v["target_date"], "metric": v["metric"], "venue": v["venue"],
                    "original_row_ids": [r["id"] for r in v["rows"]], "original_rows_hash": digest(v["rows"]),
                    "outcomes": [r["outcome"] for r in v["rows"]], "probabilities": p.tolist(), "market": v["market"],
                    "selected": model["selected"], "weight": model["weight"],
                    "mode": "shadow_only", "live_ready": False, "paper_execution_enabled": False}
        evidence["record_id"] = digest({"guard": model["model_id"], "rows": evidence["original_row_ids"]})
        path = Path(output) / "forecasts" / (evidence["record_id"] + ".json")
        if path.exists():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if (digest({k:v for k,v in prior.items() if k != "fingerprint"}) != prior.get("fingerprint")
                    or {k:v for k,v in prior.items() if k not in ("recorded_at", "fingerprint")} !=
                       {k:v for k,v in evidence.items() if k != "recorded_at"}):
                raise ValueError("GUARD_IMMUTABLE_FORECAST_CONFLICT")
            forecasts.append(prior["fingerprint"])
            continue
        evidence["fingerprint"] = digest(evidence)
        write_once(path, evidence)
        forecasts.append(evidence["fingerprint"])
    return {"quality": quality, "guard_model_id": model["model_id"], "selected": model["selected"],
            "weight": model["weight"], "forecast_vectors": len(forecasts), "fingerprints": forecasts,
            "mode": "shadow_only", "live_ready": False, "paper_execution_enabled": False}
