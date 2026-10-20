"""Offline original-run calibration and market replay; never a production model.

Archived public availability and final CLI publication times are unverified.
The fixed date splits are exploratory: some market outcomes were inspected in
earlier experiments. Nothing here promotes a model or creates forward evidence.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np

from backend.ml.weather_learning.contracts import CLI_CODES, station_metadata
from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_decision_archive import MODELS, run_pair
from backend.weather_historical_calibration import finite, standard_day, write_json
from backend.weather_market_correction import cost_diagnostics, make_panel, scores
from backend.weather_probability_screen import write_once

SPEC = {
    "version": "weather_full_archive_retest_v1", "start": "2026-04-03", "end": "2026-10-19",
    "train_end": "2026-08-29", "weather_cal_start": "2026-08-31", "weather_cal_end": "2026-09-18",
    "weather_test_start": "2026-09-20", "market_cal_start": "2026-09-20", "market_cal_end": "2026-10-01",
    "market_test_start": "2026-10-03", "ridge": 10.0, "sigma_floor_f": 1.5,
    "weights": [0.0, 0.1, 0.25, 0.5], "minimum_calibration_gain": 0.001,
    "maximum_bucket_probability_change": 0.05, "lead_days": 1,
    "stations": sorted(set(CLI_CODES.values())), "local_hours": [8, 11, 14, 17],
    "selection": "Fixed weather ridge; earlier residual calibration; market blend selected on earlier market dates only.",
}


def implementation_hash():
    return digest({p.name: p.read_text(encoding="utf-8").replace("\r\n", "\n") for p in (
        Path(__file__), Path(__file__).with_name("weather_market_correction.py"),
        Path(__file__).with_name("weather_decision_archive.py"))})


def key(row):
    return row["station"], row["target_date"], row["local_hour"]


def row_weights(rows):
    counts = Counter((r["station"], r["target_date"]) for r in rows)
    # Do not renormalize by snapshot count: that would weaken regularization
    # merely by duplicating the same event's snapshots.
    return np.array([1 / counts[(r["station"], r["target_date"])] for r in rows])


def validated_rows(rows, plan):
    if digest({k: v for k, v in plan.items() if k != "plan_id"}) != plan["plan_id"]:
        raise ValueError("ARCHIVE_PLAN_HASH_MISMATCH")
    expected = {key(r): r for r in plan["decisions"]}
    accepted, rejected, seen = [], Counter(), set()
    for row in rows:
        k = key(row)
        if k in seen:
            raise ValueError("DUPLICATE_ARCHIVE_DECISION")
        seen.add(k)
        try:
            original = expected[k]
            if any(row.get(field) != original[field] for field in ("station", "target_date", "decision_at", "local_hour", "lead_days", "runs")):
                raise ValueError("ARCHIVE_DECISION_IDENTITY_MISMATCH")
            if row.get("as_of_eligible") is not False or row.get("production_training_eligible") is not False:
                raise ValueError("ARCHIVE_ELIGIBILITY_MISLABELLED")
            if not row.get("label_receipt_hash") or not row.get("label_product") or not row.get("label_link"):
                raise ValueError("MISSING_OFFICIAL_LABEL_RECEIPT")
            if not all(finite(row.get("observed_" + m + "_f")) for m in ("high", "low")):
                raise ValueError("MISSING_OFFICIAL_EXTREMA")
            if row["observed_low_f"] > row["observed_high_f"]:
                raise ValueError("INVALID_OFFICIAL_EXTREMA")
            start, end = standard_day(row["station"], datetime.fromisoformat(row["target_date"]).date())
            if utc(row["decision_at"]) >= start:
                raise ValueError("DECISION_NOT_BEFORE_TARGET_WINDOW")
            for model, config in MODELS.items():
                current = row["forecasts"][model]["current"]
                selected, previous = run_pair(model, row["decision_at"])
                if row["runs"][model] != [r.strftime("%Y-%m-%dT%H:%M") for r in (selected, previous)]:
                    raise ValueError("ARCHIVE_RUN_SELECTION_MISMATCH")
                if (utc(current["run_initialization_utc"]) != selected
                        or utc(current["standard_day_start"]) != start or utc(current["standard_day_end"]) != end
                        or not current.get("forecast_receipt_hash")):
                    raise ValueError("ARCHIVE_RUN_OR_WINDOW_MISMATCH")
                if current.get("complete_standard_day") is not True:
                    raise ValueError("INCOMPLETE_CURRENT_FORECAST")
                if not all(finite(current.get(m + "_f")) for m in ("high", "low")):
                    raise ValueError("INVALID_FORECAST_EXTREMA")
            accepted.append(row)
        except (KeyError, TypeError, ValueError) as error:
            rejected[str(error)] += 1
    return accepted, dict(rejected)


def features(row, metric):
    forecasts = [row["forecasts"][model] for model in MODELS]
    temperatures = [f["current"][metric + "_f"] for f in forecasts]
    baseline = mean(temperatures)
    revisions = [f.get(metric + "_revision_f") for f in forecasts]
    ages = [(utc(row["decision_at"]) - utc(f["current"]["run_initialization_utc"])).total_seconds() / 43200 for f in forecasts]
    doy = datetime.fromisoformat(row["target_date"]).timetuple().tm_yday
    x = [(t - baseline) / 10 for t in temperatures]
    x += [np.std(temperatures) / 5, baseline / 100, math.sin(2 * math.pi * doy / 365.25),
          math.cos(2 * math.pi * doy / 365.25), (row["local_hour"] - 12) / 5, *ages]
    x += [v / 5 if finite(v) else 0 for v in revisions] + [int(not finite(v)) for v in revisions]
    for variable, scale in (("dew_point_2m_day_mean", 100), ("cloud_cover_day_mean", 100), ("wind_speed_10m_day_mean", 40)):
        values = [f["current"].get(variable) for f in forecasts]
        present = [v for v in values if finite(v)]
        x += [mean(present) / scale if present else 0, len(present) / 3]
    x += [int(row["station"] == s) for s in SPEC["stations"]]
    values = np.asarray(x, dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("NONFINITE_ARCHIVE_FEATURE")
    return values, baseline


def fit_metric(train, calibration, metric):
    x = np.array([features(r, metric)[0] for r in train])
    baseline = np.array([features(r, metric)[1] for r in train])
    y = np.array([r["observed_" + metric + "_f"] for r in train]) - baseline
    w = row_weights(train)
    center = np.average(x, axis=0, weights=w)
    scale = np.sqrt(np.average((x - center) ** 2, axis=0, weights=w))
    scale[scale < 1e-8] = 1
    z = (x - center) / scale
    intercept = float(np.average(y, weights=w))
    beta = np.linalg.solve(z.T @ (w[:, None] * z) + SPEC["ridge"] * np.eye(z.shape[1]), z.T @ (w * (y - intercept)))
    model = {"metric": metric, "center": center.tolist(), "scale": scale.tolist(), "beta": beta.tolist(), "intercept": intercept}
    residuals = np.array([r["observed_" + metric + "_f"] - predicted_temperature(r, model) for r in calibration])
    weights = row_weights(calibration)
    bias = float(np.average(residuals, weights=weights))
    model.update(calibration_bias_f=bias, sigma_f=max(SPEC["sigma_floor_f"], float(np.sqrt(np.average((residuals - bias) ** 2, weights=weights)))))
    return model


def predicted_temperature(row, model):
    x, baseline = features(row, model["metric"])
    return float(baseline + model["intercept"] + ((x - model["center"]) / model["scale"]) @ np.array(model["beta"]) + model.get("calibration_bias_f", 0))


def weather_scores(rows, models):
    result = {}
    weights = row_weights(rows)
    for metric, model in models.items():
        observed = np.array([r["observed_" + metric + "_f"] for r in rows])
        forecasts = {name: np.array([r["forecasts"][name]["current"][metric + "_f"] for r in rows]) for name in MODELS}
        forecasts["equal_weight_ensemble"] = np.mean(list(forecasts.values()), axis=0)
        forecasts["archive_calibrated"] = np.array([predicted_temperature(r, model) for r in rows])
        result[metric] = {name: {"mae_f": float(np.average(abs(pred - observed), weights=weights)),
                                "rmse_f": float(np.sqrt(np.average((pred - observed) ** 2, weights=weights)))} for name, pred in forecasts.items()}
    return result


def archived_probabilities(vector, source, model):
    mu, sigma = predicted_temperature(source, model), model["sigma_f"]
    cdf = lambda value: .5 * math.erfc(-(value - mu) / (sigma * math.sqrt(2)))
    p = []
    for row in vector["rows"]:
        m = row["metadata"]
        low, high = m.get("bucket_low_f"), m.get("bucket_high_f")
        p.append(max(1e-12, (1 if high is None else cdf(high)) - (0 if low is None else cdf(low))))
    return np.array(p) / sum(p)


def blend(vector, weight):
    market = np.array(vector["market"])
    delta = weight * (np.array(vector["archive_probability"]) - market)
    largest = max(abs(delta))
    if largest > SPEC["maximum_bucket_probability_change"]:
        delta *= SPEC["maximum_bucket_probability_change"] / largest
    return market + delta


def transformed(vectors, weight=None, current=False):
    # Existing scoring/cost routines accept a vector with its probability in
    # the market slot. This copy leaves the original market baseline intact.
    result = []
    for v in vectors:
        p = np.array([r["prob"] for r in v["rows"]]) if current else blend(v, weight)
        if current and (not np.isfinite(p).all() or np.any(p < 0) or not math.isclose(sum(p), 1, abs_tol=1e-5)):
            raise ValueError("INVALID_SAVED_CURRENT_PROBABILITY_VECTOR")
        result.append({**v, "market": p.tolist()})
    return result


def market_replay(rows, market_rows, models, as_of):
    panel, quality = make_panel(market_rows, as_of)
    lookup = {key(r): r for r in rows}
    vectors, rejected = [], Counter()
    for v in panel:
        decision = utc(v["decision_at"])
        local = decision.astimezone(ZoneInfo(station_metadata(v["station"])["timezone"]))
        source = lookup.get((v["station"], v["target_date"], local.hour))
        if not source or not 0 <= (decision - utc(source["decision_at"])).total_seconds() <= 1800:
            rejected["NO_MATCHING_ARCHIVE_DECISION"] += 1
            continue
        if any(utc(source["forecasts"][m]["current"]["run_initialization_utc"]) != run_pair(m, decision)[0] for m in MODELS):
            rejected["ACTUAL_DECISION_SELECTS_DIFFERENT_RUN"] += 1
            continue
        vectors.append({**v, "archive_probability": archived_probabilities(v, source, models[v["metric"]]).tolist()})
    test = [v for v in vectors if SPEC["market_test_start"] <= v["target_date"] <= SPEC["end"]]
    if not test:
        return {"status": "insufficient_matching_market_test", "quality": quality, "rejected": dict(rejected)}
    first_test = min(utc(v["decision_at"]) for v in test)
    cal = [v for v in vectors if SPEC["market_cal_start"] <= v["target_date"] <= SPEC["market_cal_end"]
           and all(utc(r["resolved_at"]) < first_test for r in v["rows"])]
    if len({v["target_date"] for v in cal}) < 3:
        return {"status": "insufficient_market_calibration_dates"}
    baseline = scores(cal)
    candidates = [{"weight": w, "scores": scores(transformed(cal, w))} for w in SPEC["weights"]]
    passing = [c for c in candidates if c["weight"] > 0 and all(c["scores"][m] < baseline[m] - SPEC["minimum_calibration_gain"] for m in ("brier", "log_loss"))]
    selected = min(passing, key=lambda c: (c["scores"]["log_loss"], c["weight"]))["weight"] if passing else 0.0
    corrected = transformed(test, selected)
    metrics = {"market": scores(test), "saved_current_model": scores(transformed(test, current=True)), "archive_correction": scores(corrected)}
    costs = cost_diagnostics(corrected, [], 0)
    accuracy_pass = selected > 0 and all(metrics["archive_correction"][m] < metrics["market"][m] for m in ("brier", "log_loss"))
    stressed = costs["additional_slippage_scenarios"]["0.03"]
    cost_pass = stressed["station_date_events"] > 0 and stressed["hypothetical_unit_pnl"] > 0
    status = "market_fallback_selected" if selected == 0 else (
        "retrospective_candidate_requires_future_validation" if accuracy_pass and cost_pass else "does_not_pass_screen")
    return {"status": status, "probability_screen_passed": accuracy_pass, "quote_cost_screen_passed": cost_pass,
            "selected_weight": selected, "calibration_candidates": candidates, "calibration_market": baseline,
            "selection_used_test_outcomes": False, "quality": quality, "rejected": dict(rejected), "test": metrics,
            "calibration_station_dates": len({(v["station"], v["target_date"]) for v in cal}),
            "test_station_dates": len({(v["station"], v["target_date"]) for v in test}),
            "test_dates": sorted({v["target_date"] for v in test}), "test_vectors": len(test),
            "by_venue": {venue: {"market": scores([v for v in test if v["venue"] == venue]),
                                  "correction": scores([v for v in corrected if v["venue"] == venue])} for venue in sorted({v["venue"] for v in test})},
            "cost_sensitivity": costs, "actual_fills_verified": False, "promotion_authorized": False}


def run(archive_directory, market_path, output):
    archive_directory, output = Path(archive_directory), Path(output)
    write_once(output / "spec.json", SPEC)
    plan = json.loads((archive_directory / "plan.json").read_text())
    status = json.loads((archive_directory / "latest.json").read_text())
    if (plan["start"] != SPEC["start"] or plan["days"] != 200 or plan["stations"] != SPEC["stations"]
            or plan["hours"] != SPEC["local_hours"] or plan["lead_days"] != SPEC["lead_days"] or status["plan_id"] != plan["plan_id"]):
        raise ValueError("RETEST_ARCHIVE_SCOPE_MISMATCH")
    if status["status"] not in ("complete", "complete_with_gaps") or status["requests_available"] != len(plan["requests"]):
        result = {"status": "awaiting_complete_archive", "requests_available": status["requests_available"],
                  "requests_planned": len(plan["requests"]), "spec_hash": digest(SPEC), "retest_performed": False}
        write_json(output / "latest.json", result)
        return result
    # Never trust a report path to escape the selected archive directory.
    if status["dataset_file"] != status["dataset_hash"] + "_dataset.json":
        raise ValueError("ARCHIVE_DATASET_PATH_INVALID")
    raw = json.loads((archive_directory / status["dataset_file"]).read_text())
    if digest(raw) != status["dataset_hash"]:
        raise ValueError("ARCHIVE_DATASET_HASH_MISMATCH")
    rows, rejected = validated_rows(raw, plan)
    train = [r for r in rows if r["target_date"] <= SPEC["train_end"]]
    cal = [r for r in rows if SPEC["weather_cal_start"] <= r["target_date"] <= SPEC["weather_cal_end"]]
    test = [r for r in rows if SPEC["weather_test_start"] <= r["target_date"] <= SPEC["end"]]
    if any(len({r["target_date"] for r in part}) < minimum for part, minimum in ((train, 90), (cal, 10), (test, 20))):
        raise ValueError("INSUFFICIENT_ARCHIVE_DATE_COVERAGE_FOR_FIXED_RETEST")
    if any(len({r["station"] for r in part}) != 21 for part in (train, cal, test)):
        raise ValueError("RETEST_REQUIRES_ALL_21_STATIONS_IN_EACH_PARTITION")
    models = {m: fit_metric(train, cal, m) for m in ("low", "high")}
    model = {"spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
             "created_at": datetime.now(timezone.utc).isoformat(), "models": models, "dataset_hash": status["dataset_hash"],
             "train_hash": digest(train), "weather_calibration_hash": digest(cal),
             "historical_availability_verified": False, "production_training_eligible": False}
    model["model_id"] = digest(model)
    market = json.loads(Path(market_path).read_text(encoding="utf-8-sig"))
    market = market["v3"] if isinstance(market, dict) else market
    result = {"status": "offline_retest_complete", "evaluated_at": datetime.now(timezone.utc).isoformat(),
              "model_id": model["model_id"], "implementation_hash": implementation_hash(), "spec_hash": digest(SPEC),
              "dataset_hash": status["dataset_hash"], "market_input_hash": digest(market), "rejected_rows": rejected,
              "coverage": {name: {"snapshots": len(part), "station_dates": len({(r['station'], r['target_date']) for r in part}),
                                  "dates": len({r["target_date"] for r in part}), "stations": len({r["station"] for r in part})}
                           for name, part in (("train", train), ("weather_calibration", cal), ("weather_test", test))},
              "weather_test": weather_scores(test, models),
              "market_test": market_replay(rows, market, models, datetime.now(timezone.utc)),
              "live_ready": False, "paper_execution_enabled": False, "profitability_proven": False,
              "limitations": ["Archived publication times and historical final-label availability are unverified.",
                              "Hourly extrema approximate continuous daily extremes; venues may use different settlement authorities.",
                              "Previously inspected market data is not a permanent unseen holdout.",
                              "High/low, venues and snapshots share station/date weight; weather across stations is correlated.",
                              "No forward forecasts, verified fills or production model changes are created."]}
    write_once(output / "models" / (model["model_id"] + ".json"), model)
    write_once(output / (digest(result) + "_report.json"), result)
    write_json(output / "latest.json", result)
    return result
