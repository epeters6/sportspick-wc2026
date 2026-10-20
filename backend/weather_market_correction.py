"""Conservative market-offset research using weather actually saved at decisions.

No live model replacement, database writes, orders, or messages. Retrospective
training is kept separate from prospective predictions and frozen v2/v3 studies.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import numpy as np
from scipy.optimize import minimize

from backend.ml.weather_learning.dataset import digest, partition_valid, prepare, utc, vector_key
from backend.ml.weather_learning.training import split_history
from backend.weather_historical_calibration import finite, standard_day
from backend.weather_probability_screen import write_once

VERSION = "weather_market_correction_v1"
SPEC = {
    "version": VERSION, "source": "weather_station_learning_v3", "scope": "all_verified_stations_high_and_low",
    "ridge_strengths": [1.0, 10.0], "correction_weights": [0.0, 0.1, 0.25, 0.5],
    "maximum_bucket_probability_change": 0.05, "maximum_logit_adjustment": 0.35,
    "minimum_calibration_gain": 0.001, "minimum_forward_dates": 45, "minimum_forward_station_dates": 250,
    "quote_max_age_seconds": 180, "minimum_edge_after_costs": 0.03,
    "extra_slippage_scenarios": [0.0, 0.01, 0.03], "minimum_visible_depth": 2,
    "bootstrap_samples": 2000, "calendar_block_days": 3, "random_seed": 91,
    "selection": "Earlier calibration dates only; fall back to market unless BOTH Brier and log loss improve.",
}
FEATURE_NAMES = ["gfs_relative_logp", "hrrr_relative_logp", "ecmwf_relative_logp", "nowcast_relative_logp",
                 "ensemble_revision_bucket_direction", "gfs_revision_bucket_direction",
                 "hrrr_revision_bucket_direction", "ecmwf_revision_bucket_direction",
                 "receipt_age_x_weather_signal", "disagreement_x_weather_signal", "lead_x_weather_signal",
                 "remaining_day_x_weather_signal", "fresh_observation_x_weather_signal", "low_x_weather_signal"]


def implementation_hash():
    files = [Path(__file__), Path(__file__).with_name("weather_probability_screen.py")]
    return digest({p.name: p.read_text(encoding="utf-8").replace("\r\n", "\n") for p in files})


def event_key(vector):
    # High/low, venues, buckets, and repeated decision hours share this weight.
    return vector["station"], vector["target_date"]


def make_panel(raw_rows, as_of, *, labelled=True):
    source = [r for r in raw_rows if r.get("source") == SPEC["source"]]
    if labelled:
        accepted, quality = prepare(source, utc(as_of))
    else:
        accepted, quality = source, {"input_rows": len(source)}
    groups = defaultdict(list)
    for row in accepted:
        groups[vector_key(row)].append(row)
    panel, rejected = [], Counter()
    # Earlier snapshots establish revisions; outcomes NEVER determine the
    # predecessor. Build the revision pool from raw known inputs, including
    # currently unlabelled vectors, then grade selected complete vectors below.
    pool = defaultdict(dict)
    for row in source:
        try:
            m = row["metadata"]
            decision = utc(m["decision_at"])
            if m.get("station_verified") is not True or decision > utc(as_of):
                continue
            if any(utc(m[k]) > decision for k in ("weather_forecast_received_at", "observations_received_at")):
                continue
            weather = m["weather_features"]
            if not all(finite(weather.get(short + "_" + m["metric"] + "_f")) for short in ("gfs", "hrrr", "ecmwf")):
                continue
            values = {short: weather[short + "_" + m["metric"] + "_f"] for short in ("gfs", "hrrr", "ecmwf")}
            key = (m["station"], m["target_date"], m["metric"])
            receipt = utc(m["weather_forecast_received_at"])
            evidence = (receipt, m["weather_forecast_hash"])
            previous = pool[key].get(evidence)
            # Conflicting snapshots of one original receipt fail closed.
            if previous and previous[1] != values:
                raise ValueError("CONFLICTING_WEATHER_RECEIPT_VALUES")
            pool[key][evidence] = (min(decision, previous[0]) if previous else decision, values)
        except (KeyError, TypeError):
            continue
    for key, group in sorted(groups.items()):
        try:
            if not partition_valid(group, labelled=labelled):
                raise ValueError("INCOMPLETE_VECTOR")
            group = sorted(group, key=lambda r: r["outcome"])
            m = group[0]["metadata"]
            station, target, metric = m["station"], m["target_date"], m["metric"]
            decision = utc(m["decision_at"])
            if decision > utc(as_of):
                raise ValueError("FUTURE_DECISION")
            start, end = standard_day(station, datetime.fromisoformat(target).date())
            if decision >= end:
                raise ValueError("AFTER_OBSERVATION_WINDOW")
            w = m["weather_features"]
            for row in group:
                meta, c = row["metadata"], row["metadata"]["contract"]
                if (utc(meta["decision_at"]) != decision or meta.get("station_verified") is not True
                        or {k: val for k, val in meta["weather_features"].items() if k != "nowcast_probability"}
                           != {k: val for k, val in w.items() if k != "nowcast_probability"}
                        or c["station"] != station or c["target_date"] != target
                        or c["metric"] != metric or c["market_id"] != row["outcome"] or c["platform"] != meta["platform"]
                        or c["authority"] != meta["settlement_source"] or utc(c["window_start"]) != start
                        or utc(c["window_end"]) != end or c.get("low_f") != meta.get("bucket_low_f")
                        or c.get("high_f") != meta.get("bucket_high_f")):
                    raise ValueError("INCONSISTENT_CONTRACT_OR_WEATHER_VECTOR")
                if not 0 <= (decision - utc(meta["quote_received_at"])).total_seconds() <= SPEC["quote_max_age_seconds"]:
                    raise ValueError("STALE_OR_FUTURE_QUOTE")
                if any(utc(meta[name]) > decision for name in ("weather_forecast_received_at", "observations_received_at")):
                    raise ValueError("INPUT_AFTER_DECISION")
                if not meta.get("weather_forecast_hash") or not meta.get("observations_hash"):
                    raise ValueError("MISSING_INPUT_RECEIPT_HASH")
            if not all(finite(w.get(short + "_" + metric + "_f")) for short in ("gfs", "hrrr", "ecmwf")):
                raise ValueError("THREE_MODEL_INPUTS_REQUIRED")
            market = [r["metadata"]["market_vector_prob"] for r in group]
            if not all(finite(p) and 0 <= p <= 1 for p in market) or not math.isclose(sum(market), 1, abs_tol=1e-6):
                raise ValueError("INVALID_MARKET_VECTOR")
            predecessors = [(known, receipt, values) for (receipt, h), (known, values) in pool[(station, target, metric)].items()
                            if known < decision and receipt < utc(m["weather_forecast_received_at"])
                            and h != m["weather_forecast_hash"] and (decision - known).total_seconds() <= 86400]
            prior = max(predecessors, key=lambda p: (p[0], p[1])) if predecessors else None
            vector = {"station": station, "target_date": target, "metric": metric, "venue": m["platform"],
                      "decision_at": decision.isoformat(), "rows": group, "market": market,
                      "previous_weather_known_at": prior[0].isoformat() if prior else None,
                      "previous_weather": prior[2] if prior else None,
                      "revision_kind": "change_in_received_forecast_snapshot_not_verified_run_revision",
                      "forecast_run_age_verified": False,
                      "weather_received_at": m["weather_forecast_received_at"],
                      "weather_receipt_hash": m["weather_forecast_hash"]}
            vector["x"] = feature_matrix(vector).tolist()
            panel.append(vector)
        except (KeyError, TypeError, ValueError) as exc:
            rejected[str(exc)] += 1
    panel.sort(key=lambda v: (v["decision_at"], v["station"], v["target_date"], v["metric"], v["venue"]))
    quality.update(accepted_vectors=len(panel), rejected_vectors=dict(rejected),
                   station_dates=len({event_key(v) for v in panel}),
                   dates=sorted({v["target_date"] for v in panel}),
                   vectors_with_earlier_weather=sum(v["previous_weather"] is not None for v in panel),
                   input_hash=digest(raw_rows), panel_hash=digest(panel))
    return panel, quality


def feature_matrix(v):
    w, metric = v["rows"][0]["metadata"]["weather_features"], v["metric"]
    means = [w[short + "_" + metric + "_f"] for short in ("gfs", "hrrr", "ecmwf")]
    sigma = max(1.5, float(w.get("prior_sigma_f") or 3))
    receipt_age = (utc(v["decision_at"]) - utc(v["weather_received_at"])).total_seconds() / 3600
    output = []
    for row, market in zip(v["rows"], v["market"]):
        m = row["metadata"]
        low, high = m.get("bucket_low_f"), m.get("bucket_high_f")
        def p(mu):
            cdf = lambda x: 0.5 * math.erfc(-(x - mu) / (sigma * math.sqrt(2)))
            return max(1e-6, (cdf(high) if high is not None else 1) - (cdf(low) if low is not None else 0))
        ratios = [float(np.clip(math.log(p(mu) / max(1e-6, market)), -4, 4)) / 4 for mu in means]
        nowcast = float(np.clip(math.log(max(1e-6, m["weather_features"]["nowcast_probability"]) / max(1e-6, market)), -4, 4)) / 4
        center = (low + high) / 2 if low is not None and high is not None else (low + 2 if low is not None else high - 2)
        direction = float(np.clip((center - mean(means)) / 10, -1, 1))
        previous = v["previous_weather"]
        changes = [float(np.clip((mu - previous[short]) / 5, -1, 1)) * direction if previous else 0
                   for mu, short in zip(means, ("gfs", "hrrr", "ecmwf"))]
        signal = mean(ratios)
        obs_age = w.get("observation_age_minutes")
        fresh = finite(obs_age) and 0 <= obs_age <= 90
        output.append([*ratios, nowcast, mean(changes), *changes,
                       min(1, receipt_age / 2) * signal, min(1, float(w.get("model_disagreement_f") or 0) / 5) * signal,
                       min(2, float(m.get("lead_days") or 0)) / 2 * signal,
                       min(1, float(w.get("remaining_hours") or 0) / 24) * signal,
                       int(fresh) * signal, int(metric == "low") * signal])
    x = np.array(output, dtype=float)
    if not np.isfinite(x).all():
        raise ValueError("NONFINITE_FEATURE")
    return x


def base_candidate(v, beta):
    market, x = np.array(v["market"]), np.array(v["x"])
    delta = np.clip(x @ np.array(beta), -SPEC["maximum_logit_adjustment"], SPEC["maximum_logit_adjustment"])
    p = market * np.exp(delta - max(delta))  # zeros remain zero; zero beta is EXACT market
    return p / p.sum()


def probabilities(v, beta, weight):
    market = np.array(v["market"])
    if weight == 0:
        return market.copy()
    delta = weight * (base_candidate(v, beta) - market)
    largest = max(abs(delta))
    if largest > SPEC["maximum_bucket_probability_change"]:
        delta *= SPEC["maximum_bucket_probability_change"] / largest
    return market + delta


def fit_offset(vectors, ridge):
    counts = Counter(event_key(v) for v in vectors)
    weights = [1 / counts[event_key(v)] / len(counts) for v in vectors]
    def objective(beta):
        value, grad = ridge * np.dot(beta, beta) / 2, ridge * beta
        for v, weight in zip(vectors, weights):
            x = np.array(v["x"])
            truth = np.array([int(r["is_correct"]) for r in v["rows"]])
            delta = x @ beta
            p = base_candidate(v, beta)
            winner_probability = float(p @ truth)
            value -= weight * math.log(max(1e-12, winner_probability))
            if winner_probability <= 1e-12:
                continue  # log-floor is constant; do not invent its derivative
            active = (abs(delta) < SPEC["maximum_logit_adjustment"]).astype(float)
            grad = grad + weight * (x.T @ ((p - truth) * active))
        return value, grad
    result = minimize(objective, np.zeros(len(FEATURE_NAMES)), jac=True, method="L-BFGS-B",
                      options={"maxiter": 150, "ftol": 1e-10})
    if not result.success or not np.isfinite(result.x).all():
        raise ValueError("OFFSET_OPTIMIZER_DID_NOT_CONVERGE")
    return result.x.tolist()


def scores(vectors, beta=None, weight=0):
    events = defaultdict(list)
    for v in vectors:
        p = probabilities(v, beta, weight) if beta is not None else np.array(v["market"])
        truth = np.array([int(r["is_correct"]) for r in v["rows"]])
        events[event_key(v)].append({"brier": float(np.sum((p - truth) ** 2)),
                                     "log_loss": -math.log(max(1e-12, float(p @ truth)))})
    return {k: mean(mean(item[k] for item in values) for values in events.values())
            for k in ("brier", "log_loss")} if events else None


def cost_diagnostics(vectors, beta, weight):
    scenarios, rejected = {}, Counter()
    for extra in SPEC["extra_slippage_scenarios"]:
        trades = {}
        for v in vectors:
            if event_key(v) in trades:
                continue
            eligible = []
            for row, p in zip(v["rows"], probabilities(v, beta, weight)):
                m = row["metadata"]
                cost, depth, fee = m.get("executable_cost"), m.get("ask_depth"), m.get("fee_estimate")
                try:
                    if not finite(cost) or not 0 < cost < 1 or not finite(depth) or depth < SPEC["minimum_visible_depth"]:
                        raise ValueError("MISSING_COST_OR_INSUFFICIENT_DEPTH")
                    if not isinstance(fee, dict) or not fee.get("fee_source") or not finite(fee.get("fee_per_share")) or fee["fee_per_share"] < 0:
                        raise ValueError("MISSING_FEE_EVIDENCE")
                    if not fee.get("fee_schedule_checked_at"):
                        raise ValueError("MISSING_FEE_TIMESTAMP")
                    if not 0 <= (utc(v["decision_at"]) - utc(fee["fee_schedule_checked_at"])).total_seconds() <= 300:
                        raise ValueError("STALE_FEE_EVIDENCE")
                    if not 0 <= (utc(v["decision_at"]) - utc(m["quote_received_at"])).total_seconds() <= SPEC["quote_max_age_seconds"]:
                        raise ValueError("STALE_QUOTE")
                    if p - (cost + extra) >= SPEC["minimum_edge_after_costs"]:
                        eligible.append({"station": v["station"], "target_date": v["target_date"], "venue": v["venue"],
                                         "decision_at": v["decision_at"], "outcome": row["outcome"], "edge": float(p - cost - extra),
                                         "cost": cost + extra, "hypothetical_unit_pnl": int(row["is_correct"]) - cost - extra})
                except (KeyError, TypeError, ValueError) as error:
                    if extra == 0:
                        rejected[str(error)] += 1
            if eligible:
                trades[event_key(v)] = sorted(eligible, key=lambda t: (-t["edge"], t["outcome"]))[0]
        ledger = list(trades.values())
        # Taker-at-recorded-ask sensitivity, NOT an assertion that quotes filled.
        scenarios[str(extra)] = {"station_date_events": len(ledger),
            "hypothetical_unit_pnl": sum(t["hypothetical_unit_pnl"] for t in ledger),
            "unit_cost": sum(t["cost"] for t in ledger), "zero_fill_pnl": 0.0,
            "adverse_fill_subset_pnl": sum(min(0, t["hypothetical_unit_pnl"]) for t in ledger),
            "ledger": ledger}
    return {"type": "quote_only_taker_sensitivity", "fills_verified": False, "profitability_verified": False,
            "saved_cost_includes": "recorded ask plus original fee estimate and original 0.005 slippage buffer",
            "additional_slippage_scenarios": scenarios, "rejected_rows": dict(rejected),
            "limitations": ["Displayed size is not a fill guarantee; require two displayed contracts for a one-contract diagnostic.",
                            "Spread is paid through the recorded ask; historical full books and order latency are unverified.",
                            "Zero-fill and adverse-selection bounds are scenarios, not fill models.",
                            "One first eligible contract per station/date across metrics and venues; no bankroll or realized P&L."]}


def chronological_parts(panel):
    parts = split_history([r for v in panel for r in v["rows"]])
    id_parts = [{r["id"] for r in part} for part in parts]
    result = [[v for v in panel if all(r["id"] in ids for r in v["rows"])] for ids in id_parts]
    if any(not part for part in result):
        raise ValueError("INSUFFICIENT_COMPLETE_CHRONOLOGICAL_PARTITIONS")
    return result


def fit_select(train, calibration):
    baseline = scores(calibration)
    candidates = []
    for ridge in SPEC["ridge_strengths"]:
        beta = fit_offset(train, ridge)
        for weight in SPEC["correction_weights"]:
            candidates.append({"ridge": ridge, "weight": weight, "beta": beta, "calibration": scores(calibration, beta, weight)})
    eligible = [c for c in candidates if c["weight"] > 0 and all(
        c["calibration"][metric] < baseline[metric] - SPEC["minimum_calibration_gain"] for metric in ("brier", "log_loss"))]
    chosen = min(eligible, key=lambda c: (c["calibration"]["log_loss"], c["weight"], -c["ridge"])) if eligible else {
        "ridge": None, "weight": 0.0, "beta": [0.0] * len(FEATURE_NAMES), "calibration": baseline}
    return chosen, {"market": baseline, "candidates": candidates, "selection_used_test_outcomes": False}


def paired_uncertainty(vectors, beta, weight):
    events = defaultdict(list)
    for v in vectors:
        baseline = scores([v])["brier"]
        corrected = scores([v], beta, weight)["brier"]
        events[event_key(v)].append(baseline - corrected)
    dates = sorted({v["target_date"] for v in vectors})
    start = datetime.fromisoformat(dates[0]).date()
    blocks = defaultdict(list)
    for (_, target), values in events.items():
        index = (datetime.fromisoformat(target).date() - start).days // SPEC["calendar_block_days"]
        blocks[index].append(mean(values))
    result = {"calendar_dates": len(dates), "calendar_blocks": len(blocks),
              "brier_advantage_over_market": mean(mean(v) for v in events.values()),
              "forward_evidence": False}
    if len(blocks) < 4:
        return {**result, "status": "insufficient_calendar_blocks", "interval_95": None}
    rng, values = np.random.default_rng(SPEC["random_seed"]), list(blocks.values())
    draws = [mean(x for i in rng.integers(len(values), size=len(values)) for x in values[i])
             for _ in range(SPEC["bootstrap_samples"])]
    return {**result, "status": "exploratory", "interval_95": np.quantile(draws, [.025, .975]).tolist()}


def verify_artifact(model):
    if (digest({k: v for k, v in model.items() if k != "model_id"}) != model.get("model_id")
            or model.get("implementation_hash") != implementation_hash() or model.get("spec_hash") != digest(SPEC)):
        raise ValueError("CORRECTION_ARTIFACT_IDENTITY_MISMATCH")


def predict_forward(panel, model):
    verify_artifact(model)
    if any(utc(v["decision_at"]) <= utc(model["created_at"]) or v["target_date"] <= model["training_last_target"] for v in panel):
        raise ValueError("MODEL_NOT_AVAILABLE_BEFORE_FORWARD_DECISION")
    return [probabilities(v, model["beta"], model["weight"]).tolist() for v in panel]


def run(input_path, output, *, as_of=None):
    now = utc(as_of or datetime.now(timezone.utc))
    output = Path(output)
    write_once(output / "spec.json", SPEC)
    raw = json.loads(Path(input_path).read_text(encoding="utf-8-sig"))
    raw = raw["v3"] if isinstance(raw, dict) else raw
    panel, quality = make_panel(raw, now)
    train, cal, test = chronological_parts(panel)
    chosen, selection = fit_select(train, cal)
    report = {"version": VERSION, "spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
              "evaluated_at": now.isoformat(), "mode": "offline_research", "live_ready": False,
              "paper_execution_enabled": False, "forward_predictions_created": 0, "quality": quality,
              "selected_weight": chosen["weight"], "selected_ridge": chosen["ridge"], "selection": selection,
              "split": {name: {"vectors": len(part), "station_dates": len({event_key(v) for v in part}),
                                  "target_dates": sorted({v["target_date"] for v in part}),
                                  "first_decision": min(v["decision_at"] for v in part),
                                  "last_label": max(r["resolved_at"] for v in part for r in v["rows"])}
                        for name, part in zip(("train", "calibration", "test"), (train, cal, test))},
              "test": {"market": scores(test), "correction": scores(test, chosen["beta"], chosen["weight"])},
              "cost_sensitivity": cost_diagnostics(test, chosen["beta"], chosen["weight"]),
              "market_cost_sensitivity": cost_diagnostics(test, [0.0] * len(FEATURE_NAMES), 0.0),
              "paired_uncertainty": paired_uncertainty(test, chosen["beta"], chosen["weight"]),
              "limitations": ["Retrospective research: the fitted model did not exist at these old decisions.",
                              "This previously inspected data is not a permanent unseen holdout.",
                              "Saved receipts prove inputs were captured before decisions; they do not establish individual run age.",
                              "Archive-only data with assumed publication times is excluded from this model.",
                              "Few calendar dates and shared weather limit effective sample size."]}
    accuracy_passed = chosen["weight"] > 0 and all(report["test"]["correction"][k] < report["test"]["market"][k] for k in ("brier", "log_loss"))
    stressed = report["cost_sensitivity"]["additional_slippage_scenarios"]["0.03"]
    cost_passed = stressed["station_date_events"] > 0 and stressed["hypothetical_unit_pnl"] > 0
    passed = accuracy_passed and cost_passed
    report["screen_gates"] = {"later_date_probability_improvement": accuracy_passed,
                               "positive_quote_diagnostic_under_extra_three_cent_slippage": cost_passed,
                               "actual_fills_verified": False, "promotion_authorized": False}
    report["status"] = "candidate_for_separate_prospective_review" if passed else (
        "market_fallback_selected" if chosen["weight"] == 0 else "does_not_pass_later_date_screen")
    # An evaluation cutoff may be historical; artifact creation must never be
    # backdated to make a retrospective fit appear available to old decisions.
    model = {"version": VERSION, "spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
             "created_at": datetime.now(timezone.utc).isoformat(), "training_last_target": max(v["target_date"] for v in train + cal),
             "training_panel_hash": digest(train), "calibration_panel_hash": digest(cal), "feature_names": FEATURE_NAMES,
             "beta": chosen["beta"], "weight": chosen["weight"], "mode": "shadow_candidate",
             "live_ready": False, "paper_execution_enabled": False}
    model["model_id"] = digest(model)
    report["model_id"] = model["model_id"]
    write_once(output / "models" / (model["model_id"] + ".json"), model)
    write_once(output / (digest(report) + "_report.json"), report)
    # Compact audit links to the original immutable rows; no duplicate full history.
    audit = [{k: v[k] for k in ("station", "target_date", "metric", "venue", "decision_at", "weather_receipt_hash",
                               "weather_received_at", "previous_weather_known_at", "revision_kind", "forecast_run_age_verified")}
             | {"row_ids": [r["id"] for r in v["rows"]]} for v in panel]
    write_once(output / (quality["panel_hash"] + "_input_audit.json"), audit)
    return report
