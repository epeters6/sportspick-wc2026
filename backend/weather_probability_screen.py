"""Offline probability screening, separate from every frozen/forward experiment.

Uses immutable original GFS runs and final NWS CLI observations. Neither the
archive's historical public availability nor historical CLI publication times
are known. Results are retrospective diagnostics, never forward predictions.
"""
from __future__ import annotations

import json
import hashlib
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np

from backend.ml.weather_learning.contracts import CLI_CODES, station_metadata
from backend.ml.weather_learning.dataset import digest, partition_valid, prepare, utc, vector_key
from backend.weather_historical_calibration import finite, standard_day

VERSION = "weather_low_probability_screen_v1"
# Chosen before scoring the saved market vectors. Do not tune on these results.
SPEC = {
    "version": VERSION, "metric": "low", "lead_days": 1,
    "forecast_model": "gfs_global", "run_hour_utc": 0,
    "run_days_before_target": 1, "window": "local_standard_day",
    "training_target_end": "2026-08-29", "evaluation_target_start": "2026-08-30",
    "minimum_training_dates_per_station": 90,
    "residual_kernel_sigma_f": 1.0,
    "candidates": {"station_residual": 1.0, "half_market_half_residual": 0.5},
    "quote_diagnostic_minimum_edge": 0.05,
    "uncertainty_block_calendar_days": 3, "bootstrap_samples": 2000, "random_seed": 72,
    "decision": "Screen weak candidates early; no automated promotion or station selection.",
}


def screen_implementation_hash():
    # Separate identity: this file is intentionally outside weather_learning/.
    return hashlib.sha256(Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n").encode()).hexdigest()


def write_once(path, value):
    """Never replace a model, specification, or result under the same identity."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("IMMUTABLE_SCREEN_FILE_CONFLICT")
        return
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def validate_history(rows, expected_hash):
    if not expected_hash or digest(rows) != expected_hash:
        raise ValueError("HISTORICAL_DATASET_HASH_MISMATCH")
    by_event = {}
    for row in rows:
        station, target = row["station"], date.fromisoformat(row["target_date"])
        if station not in CLI_CODES.values() or row["model"] != SPEC["forecast_model"]:
            raise ValueError("HISTORICAL_STATION_OR_MODEL_MISMATCH")
        start, end = standard_day(station, target)
        expected_run = datetime.combine(target - timedelta(days=1), datetime.min.time(), timezone.utc)
        if (utc(row["run_initialization_utc"]) != expected_run
                or utc(row["standard_day_start"]) != start or utc(row["standard_day_end"]) != end):
            raise ValueError("HISTORICAL_RUN_OR_WINDOW_MISMATCH")
        if not all(finite(row[k]) for k in ("forecast_low_f", "observed_low_f")):
            raise ValueError("INVALID_TEMPERATURE")
        if not all(row.get(k) for k in ("forecast_receipt_hash", "label_receipt_hash", "label_product", "label_link")):
            raise ValueError("MISSING_HISTORICAL_PROVENANCE")
        if row.get("as_of_eligible") is not False or row.get("production_training_eligible") is not False:
            raise ValueError("HISTORICAL_ELIGIBILITY_CHANGED")
        key = (station, target.isoformat())
        if key in by_event:
            raise ValueError("DUPLICATE_STATION_DATE")
        by_event[key] = row
    return by_event


def fit(rows, *, target_end, created_at):
    """Chronological station residuals; accepts only validated matched history."""
    if date.fromisoformat(target_end) >= utc(created_at).date():
        raise ValueError("TRAINING_TARGET_NOT_COMPLETED")
    selected = [r for r in rows if r["target_date"] <= target_end]
    residuals = defaultdict(list)
    for row in selected:
        # Final observations were available to us only when their receipt was
        # obtained. The current timestamp is genuine, never backdated to August.
        if utc(row["retrieved_at"]) > utc(created_at):
            raise ValueError("TRAINING_DATA_RECEIVED_AFTER_MODEL_CREATION")
        residuals[row["station"]].append(row["observed_low_f"] - row["forecast_low_f"])
    supported = {s: values for s, values in sorted(residuals.items())
                 if len(values) >= SPEC["minimum_training_dates_per_station"]}
    if not supported:
        raise ValueError("INSUFFICIENT_TRAINING_DATES")
    artifact = {"version": VERSION, "spec_hash": digest(SPEC), "created_at": utc(created_at).isoformat(),
                "implementation_hash": screen_implementation_hash(),
                "training_target_end": target_end, "training_data_hash": digest(selected),
                "training_station_dates": len(selected), "residuals_f": supported,
                "sigma_f": SPEC["residual_kernel_sigma_f"], "mode": "offline_research",
                "live_ready": False, "paper_execution_enabled": False,
                "historical_as_of_verified": False}
    return {**artifact, "model_id": digest(artifact)}


def verify_model(model):
    body = {k: v for k, v in model.items() if k != "model_id"}
    if (digest(body) != model.get("model_id") or model.get("spec_hash") != digest(SPEC)
            or model.get("implementation_hash") != screen_implementation_hash()):
        raise ValueError("MODEL_ID_OR_SPEC_MISMATCH")
    if model.get("sigma_f") != SPEC["residual_kernel_sigma_f"]:
        raise ValueError("MODEL_KERNEL_MISMATCH")


def probability_vector(group, forecast_low_f, model):
    if not partition_valid(group) or not finite(forecast_low_f):
        raise ValueError("INCOMPLETE_PARTITION_OR_FORECAST")
    station = group[0]["metadata"]["station"]
    if any(r["metadata"]["station"] != station for r in group):
        raise ValueError("MIXED_STATIONS")
    residuals = model["residuals_f"].get(station)
    if not residuals:
        raise ValueError("STATION_NOT_TRAINED")
    scale = model["sigma_f"] * math.sqrt(2)

    def cdf(bound, upper):
        if bound is None:
            return 1.0 if upper else 0.0
        return mean(0.5 * math.erfc(-(bound - forecast_low_f - error) / scale) for error in residuals)

    probabilities = [max(0.0, cdf(r["metadata"].get("bucket_high_f"), True)
                              - cdf(r["metadata"].get("bucket_low_f"), False)) for r in group]
    if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-8):
        raise ValueError("PROBABILITY_VECTOR_NOT_NORMALIZED")
    return probabilities


def score(group, probabilities):
    return {"brier": sum((p - int(row["is_correct"])) ** 2 for row, p in zip(group, probabilities)),
            "log_loss": -math.log(max(1e-12, next(p for row, p in zip(group, probabilities) if row["is_correct"])))}


def summarize(vectors):
    events = defaultdict(list)
    for vector in vectors:
        events[(vector["station"], vector["target_date"])].append(vector)
    names = ["market", *SPEC["candidates"]]
    return {name: {metric: mean(mean(v["scores"][name][metric] for v in event) for event in events.values())
                   for metric in ("brier", "log_loss")} for name in names} if events else {}


def advancement_decisions(summary):
    if not summary:
        return {name: "insufficient_evidence" for name in SPEC["candidates"]}
    return {name: ("does_not_pass_screen" if any(summary[name][metric] >= summary["market"][metric]
                                               for metric in ("brier", "log_loss"))
                   else "requires_new_frozen_prospective_trial_no_automatic_promotion")
            for name in SPEC["candidates"]}


def uncertainty(vectors, candidate):
    """Paired calendar-block bootstrap; keep all stations/venues in each date."""
    events = defaultdict(list)
    for vector in vectors:
        events[(vector["station"], vector["target_date"])].append(
            vector["scores"]["market"]["brier"] - vector["scores"][candidate]["brier"])
    if not events:
        return {"status": "no_events"}
    first = date.fromisoformat(min(key[1] for key in events))
    blocks = defaultdict(list)
    for (_, target), values in events.items():
        block = (date.fromisoformat(target) - first).days // SPEC["uncertainty_block_calendar_days"]
        blocks[block].append(mean(values))
    values = list(blocks.values())
    if len(values) < 4:
        return {"status": "insufficient_calendar_blocks", "blocks": len(values)}
    rng = np.random.default_rng(SPEC["random_seed"])
    draws = [mean(x for i in rng.integers(len(values), size=len(values)) for x in values[i])
             for _ in range(SPEC["bootstrap_samples"])]
    return {"status": "exploratory", "brier_advantage_over_market": mean(mean(v) for v in events.values()),
            "calendar_blocks": len(values), "interval_95": np.quantile(draws, [.025, .975]).tolist(),
            "multiplicity_adjusted": False, "independent_future_evidence": False}


def quote_diagnostic(group, probabilities, decision):
    eligible = []
    for row, p in zip(group, probabilities):
        m = row["metadata"]
        cost, depth, fee = m.get("executable_cost"), m.get("ask_depth"), m.get("fee_estimate")
        if not finite(cost) or not 0 < cost < 1 or not finite(depth) or depth < 1 or not isinstance(fee, dict):
            continue
        try:
            age = (decision - utc(m["quote_received_at"])).total_seconds()
            fee_age = (decision - utc(fee["fee_schedule_checked_at"])).total_seconds()
            if not 0 <= age <= 180 or not 0 <= fee_age <= 300 or not fee.get("fee_source"):
                continue
            if not finite(fee.get("fee_per_share")) or fee["fee_per_share"] < 0:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if p - cost >= SPEC["quote_diagnostic_minimum_edge"]:
            eligible.append({"outcome": row["outcome"], "unit_cost": cost, "edge": p - cost,
                             "hypothetical_unit_pnl": int(row["is_correct"]) - cost})
    return sorted(eligible, key=lambda x: (-x["edge"], x["outcome"]))[0] if eligible else None


def replay(history, market_rows, model, *, as_of):
    verify_model(model)
    if model["training_target_end"] >= SPEC["evaluation_target_start"]:
        raise ValueError("TRAIN_TEST_DATE_OVERLAP")
    accepted, quality = prepare(market_rows, utc(as_of))
    groups = defaultdict(list)
    for row in accepted:
        groups[vector_key(row)].append(row)
    vectors, rejected = [], Counter()
    for key, group in sorted(groups.items()):
        venue, station, target, metric, _ = key
        try:
            if metric != "low" or target < SPEC["evaluation_target_start"]:
                raise ValueError("OUTSIDE_PREDECLARED_SCOPE")
            source = history.get((station, target))
            if source is None:
                raise ValueError("NO_MATCHING_ARCHIVED_FORECAST")
            decisions = {utc(r["metadata"]["decision_at"]) for r in group}
            if len(decisions) != 1:
                raise ValueError("INCONSISTENT_VECTOR_DECISION")
            decision = decisions.pop()
            local = decision.astimezone(ZoneInfo(station_metadata(station)["timezone"]))
            if (date.fromisoformat(target) - local.date()).days != 1 or local.hour not in (8, 11, 14, 17):
                raise ValueError("NOT_NEXT_DAY_ELIGIBLE_DECISION")
            if decision.date().isoformat() <= model["training_target_end"]:
                raise ValueError("TRAINING_AFTER_REPLAY_DECISION")
            for row in group:
                contract = row["metadata"]["contract"]
                m = row["metadata"]
                if (contract["station"] != station or contract["target_date"] != target or contract["metric"] != metric
                        or contract["market_id"] != row["outcome"] or contract["platform"] != venue
                        or contract["authority"] != m["settlement_source"]
                        or utc(contract["window_start"]) != utc(source["standard_day_start"])
                        or utc(contract["window_end"]) != utc(source["standard_day_end"])
                        or contract.get("low_f") != m.get("bucket_low_f")
                        or contract.get("high_f") != m.get("bucket_high_f")):
                    raise ValueError("CONTRACT_PROVENANCE_MISMATCH")
                if not 0 <= (decision - utc(m["quote_received_at"])).total_seconds() <= 180:
                    raise ValueError("STALE_OR_FUTURE_QUOTE")
                if not utc(source["run_initialization_utc"]) < decision < utc(contract["window_start"]):
                    raise ValueError("RUN_OR_OBSERVATION_TIMING_INVALID")
            market = [r["metadata"]["market_vector_prob"] for r in group]
            if not math.isclose(sum(market), 1.0, abs_tol=1e-6):
                raise ValueError("MARKET_VECTOR_NOT_NORMALIZED")
            model_p = probability_vector(group, source["forecast_low_f"], model)
            predictions = {"market": market, **{name: [weight * p + (1 - weight) * q for p, q in zip(model_p, market)]
                                               for name, weight in SPEC["candidates"].items()}}
            observed = source["observed_low_f"]
            cli_matches = all(r["is_correct"] == ((r["metadata"].get("bucket_low_f") is None or observed >= r["metadata"]["bucket_low_f"])
                             and (r["metadata"].get("bucket_high_f") is None or observed < r["metadata"]["bucket_high_f"])) for r in group)
            vectors.append({"station": station, "target_date": target, "venue": venue,
                            "settlement_authority": group[0]["metadata"]["settlement_source"],
                            "decision_at": decision.isoformat(), "original_run": source["run_initialization_utc"],
                            "market_row_ids": [r["id"] for r in group], "cli_matches_venue_winner": cli_matches,
                            "scores": {name: score(group, p) for name, p in predictions.items()},
                            "quote_diagnostics": {name: quote_diagnostic(group, p, decision) for name, p in predictions.items()}})
        except (KeyError, TypeError, ValueError) as exc:
            rejected[str(exc)] += 1
    vectors.sort(key=lambda v: (v["decision_at"], v["venue"], v["station"], v["target_date"]))
    quote_results = {}
    for name in ["market", *SPEC["candidates"]]:
        chosen = {}
        for vector in vectors:
            diagnostic = vector["quote_diagnostics"][name]
            if diagnostic:
                chosen.setdefault((vector["station"], vector["target_date"]), diagnostic)
        quote_results[name] = {"station_date_events": len(chosen),
            "hypothetical_unit_pnl": sum(v["hypothetical_unit_pnl"] for v in chosen.values()),
            "unit_cost": sum(v["unit_cost"] for v in chosen.values()),
            "fills_verified": False, "realized_profit": False,
            "rule": "First qualifying snapshot; highest edge bucket; one contract per station/date across venues."}
    events = {(v["station"], v["target_date"]) for v in vectors}
    summary = summarize(vectors)
    return {"status": "retrospective_screen_complete" if vectors else "insufficient_matching_evidence",
            "mode": "offline_research", "live_ready": False, "paper_execution_enabled": False,
            "historical_as_of_verified": False, "forward_forecasts_created": 0, "profitability_proven": False,
            "model_id": model["model_id"], "model_created_at": model["created_at"], "spec": SPEC,
            "market_input_hash": digest(market_rows), "input_quality": quality,
            "rejected_vectors": dict(rejected), "vectors": len(vectors), "station_date_events": len(events),
            "calendar_dates": sorted({t for _, t in events}), "stations": sorted({s for s, _ in events}),
            "scores": summary, "candidate_decisions": advancement_decisions(summary),
            "paired_uncertainty": {n: uncertainty(vectors, n) for n in SPEC["candidates"]},
            "by_venue_and_authority": {str(k): summarize([v for v in vectors if (v["venue"], v["settlement_authority"]) == k])
                                       for k in sorted({(v["venue"], v["settlement_authority"]) for v in vectors})},
            "by_station": {s: summarize([v for v in vectors if v["station"] == s]) for s in sorted({s for s, _ in events})},
            "cli_venue_disagreement_events": len({(v["station"], v["target_date"]) for v in vectors if not v["cli_matches_venue_winner"]}),
            "quote_only_diagnostics": quote_results,
            "limitations": ["Historical run initialization does not establish public availability at the old decision.",
                "Final NWS observations are not historical label-publication receipts.",
                "NWS-trained probabilities are scored against each venue's official winner, including Weather Company differences.",
                "Low-temperature scope was selected after viewing earlier temperature-error results; this is exploratory.",
                "Two fixed candidates are screened without test-set tuning; station breakdowns do not select trading stations.",
                "Snapshots, buckets and venues share one station/date weight; spatial dependence remains.",
                "The block-bootstrap interval is exploratory, especially with few calendar blocks; it is not a readiness test.",
                "Quote diagnostics omit unverified fills, slippage and operational costs; they are not profit."],
            "vector_evidence": vectors}


def run(history_path, market_path, output, *, expected_history_hash, as_of=None):
    now = utc(as_of or datetime.now(timezone.utc))
    output = Path(output)
    write_once(output / "spec.json", SPEC)  # freeze the specification before scoring
    history_rows = json.loads(Path(history_path).read_text(encoding="utf-8-sig"))
    history = validate_history(history_rows, expected_history_hash)
    market = json.loads(Path(market_path).read_text(encoding="utf-8-sig"))
    if isinstance(market, dict):
        market = market["v3"]
    model = fit(history_rows, target_end=SPEC["training_target_end"], created_at=now)
    write_once(output / "models" / (model["model_id"] + ".json"), model)
    result = replay(history, market, model, as_of=now)
    result.update(historical_dataset_hash=expected_history_hash, evaluated_at=now.isoformat(), spec_hash=digest(SPEC),
                  implementation_hash=screen_implementation_hash())
    evidence = result.pop("vector_evidence")
    result["vector_evidence_hash"] = digest(evidence)
    identity = digest(result)
    write_once(output / (identity + "_vectors.json"), evidence)
    write_once(output / (identity + "_report.json"), result)
    return result
