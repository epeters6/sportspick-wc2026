"""Audit saved forward forecasts without refitting or altering frozen models."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np

from backend.ml.weather_learning import SOURCE
from backend.ml.weather_learning.dataset import digest, finite, prepare, utc, vector_key

MODELS = ("regularized_logistic", "boosted_trees")


def event_key(vector):
    return vector["station"], vector["target_date"]


def probability_vector(values):
    return (all(finite(p) is not None and 0 <= p <= 1 for p in values)
            and math.isclose(sum(values), 1, abs_tol=1e-6))


def forward_panel(raw, as_of, *, labelled=True):
    """All comparisons use the same complete, pre-outcome decision vectors."""
    source = [r for r in raw if r.get("source") == SOURCE]
    if labelled:
        accepted, quality = prepare(source, utc(as_of))
    else:
        accepted, quality = source, {"input_rows": len(source)}
    groups = defaultdict(list)
    for row in accepted:
        groups[vector_key(row)].append(row)
    panel, rejected = [], Counter()
    for group in groups.values():
        try:
            from backend.ml.weather_learning.dataset import partition_valid
            if not partition_valid(group, labelled=labelled):
                raise ValueError("INCOMPLETE_VECTOR")
            rows = sorted(group, key=lambda r: r["outcome"])
            m = rows[0]["metadata"]
            decision = utc(m["decision_at"])
            if decision > utc(as_of):
                raise ValueError("FUTURE_DECISION")
            for row in rows:
                meta = row["metadata"]
                if (meta.get("station_verified") is not True or utc(meta["decision_at"]) != decision
                        or meta.get("model_id") != m.get("model_id")
                        or meta.get("model_trained_at") != m.get("model_trained_at")):
                    raise ValueError("INCONSISTENT_MODEL_OR_DECISION")
                if not meta.get("model_id") or not meta.get("model_trained_at") or utc(meta["model_trained_at"]) >= decision:
                    raise ValueError("MODEL_NOT_AVAILABLE_AT_DECISION")
                if any(utc(meta[k]) > decision for k in ("weather_forecast_received_at", "observations_received_at", "quote_received_at")):
                    raise ValueError("INPUT_AFTER_DECISION")
            market = [r["metadata"]["market_vector_prob"] for r in rows]
            predictions = {name: [r["metadata"]["challenger_probabilities"][name] for r in rows] for name in MODELS}
            if not all(probability_vector(p) for p in [market, *predictions.values()]):
                raise ValueError("INVALID_PROBABILITY_VECTOR")
            weather = m.get("weather_features") or {}
            age = finite(weather.get("observation_age_minutes"))
            panel.append({"station": m["station"], "target_date": m["target_date"], "metric": m["metric"],
                          "venue": m["platform"], "lead_days": m.get("lead_days"),
                          "local_standard_hour": decision.astimezone(ZoneInfo(m["observation_timezone"])).hour,
                          "observation": "fresh" if age is not None and 0 <= age <= 90 else "missing_or_stale",
                          "model_id": m["model_id"], "decision_at": decision.isoformat(),
                          "model_trained_at": m["model_trained_at"], "market": market,
                          "predictions": predictions, "rows": rows})
        except (KeyError, TypeError, ValueError) as error:
            rejected[str(error)] += 1
    panel.sort(key=lambda v: (v["decision_at"], v["station"], v["target_date"], v["metric"], v["venue"]))
    quality.update(matched_vectors=len(panel), matched_station_dates=len({event_key(v) for v in panel}),
                   matched_dates=len({v["target_date"] for v in panel}), rejected_vectors=dict(rejected),
                   input_hash=digest(raw))
    return panel, quality


def summarize(panel):
    """Equal station/date weight; high/low, venues and snapshots share it."""
    output = {}
    for name in ("market", *MODELS):
        events = defaultdict(list)
        for v in panel:
            truth = np.array([int(r["is_correct"]) for r in v["rows"]])
            market = np.array(v["market"])
            p = market if name == "market" else np.array(v["predictions"][name])
            delta = p - market
            events[event_key(v)].append({
                "brier": float(np.sum((p - truth) ** 2)),
                "log_loss": -math.log(max(1e-12, float(p @ truth))),
                "brier_change_vs_market": float(np.sum((p - truth) ** 2) - np.sum((market - truth) ** 2)),
                "adjustment_penalty": float(delta @ delta),
                "directional_term": float(2 * ((market - truth) @ delta)),
                "absolute_probability_change": float(np.abs(delta).sum() / 2),
            })
        output[name] = {key: mean(mean(s[key] for s in values) for values in events.values())
                        for key in next(iter(events.values()))[0]} if events else None
    return {"station_dates": len({event_key(v) for v in panel}), "dates": len({v["target_date"] for v in panel}),
            "vectors": len(panel), "scores": output}


def diagnose(raw, as_of=None):
    now = utc(as_of or datetime.now(timezone.utc))
    panel, quality = forward_panel(raw, now)
    slices = {}
    for field in ("lead_days", "local_standard_hour", "metric", "venue", "observation", "model_id"):
        groups = defaultdict(list)
        for v in panel:
            groups[str(v[field])].append(v)
        slices[field] = {key: summarize(value) for key, value in sorted(groups.items())}
    costs = Counter()
    for v in panel:
        for row in v["rows"]:
            m = row["metadata"]
            fee = m.get("fee_estimate") or {}
            cost, depth = finite(m.get("executable_cost")), finite(m.get("ask_depth"))
            if cost is None or not 0 < cost < 1 or depth is None or depth < 2:
                costs["missing_cost_or_insufficient_depth"] += 1
            elif not fee.get("fee_schedule_checked_at"):
                costs["missing_fee_timestamp"] += 1
            else:
                costs["cost_and_timestamp_present_not_fill_proof"] += 1
    return {"version": "weather_model_diagnostics_v1", "as_of": now.isoformat(), "quality": quality,
            "overall": summarize(panel), "slices": slices, "cost_evidence_rows": dict(costs),
            "mode": "read_only_diagnostic", "live_ready": False, "paper_execution_enabled": False,
            "limitations": ["Slice comparisons are descriptive; do not select cities/hours on these inspected outcomes.",
                            "Brier change equals directional term plus adjustment penalty; this is attribution, not a causal experiment.",
                            "Repeated decisions and high/low share station/date weight; weather across cities can remain correlated.",
                            "No executed profit or fill claim; missing cost evidence remains missing."]}
