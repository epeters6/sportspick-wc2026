"""Separate offline daytime uncertainty calibration and market disagreement guard.

No network, operational model writes, orders, or historical forward claims.
The completed GFS archive is read-only; the hosted multi-model archive is not used.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np
import scipy
from scipy.special import ndtr, ndtri

from backend import weather_adaptive_calibration as adaptive
from backend import weather_residual_calibration as residual
from backend import weather_station_distribution as static
from backend.ml.weather_learning.contracts import station_metadata
from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_historical_calibration import standard_day
from backend.weather_model_diagnostics import forward_panel
from backend.weather_probability_screen import write_once

SPEC = {
    "version": "weather_daytime_challenger_v1", "metric": "high",
    "stations": list(static.SPEC["stations"]), "start": "2026-04-03", "end": "2026-10-18",
    "temperature_test_start": "2026-09-20", "assumed_label_lag_days": 3,
    "mean": "PR20 rolling residual recentering; unchanged PR19 reference width",
    "shape": "Symmetric piecewise-normal quantile warp of earlier standardized prediction errors",
    "shape_window_days": 60, "minimum_shape_station_dates": 21, "shape_pool_prior": 30,
    "coverages": [.5, .8, .9, .95], "shape_scale_bounds": [.5, 1.5],
    "market_cal_start": "2026-09-21", "market_cal_end": "2026-09-29",
    "market_test_start": "2026-10-01", "market_test_end": "2026-10-18",
    "minimum_guard_dates": 7, "minimum_guard_station_dates": 75,
    "minimum_band_station_dates": 40, "market_probability_bins": [0., .05, .15, .35, .65, 1.000001],
    "weights": [0., .1, .25, .5], "maximum_probability_change": .05,
    "minimum_gain": .001, "minimum_fraction_dates_improved": .6,
    "bootstrap_calendar_days": 3, "bootstrap_samples": 2000, "seed": 231,
    "minimum_edge_after_cost": .05, "extra_slippage": [0., .01, .03],
    "decision_hours_local": [8, 11, 14, 17],
    "sampling": "First valid saved next-day high vector per venue/station/date; venues share station/date weight",
    "selection": "Earlier calibration labels only; both proper scores and positive-adjustment price bands must pass; otherwise market",
    "evaluation": "Exploratory replay of previously inspected months, never an unseen holdout",
    "mode": "offline_research", "live_ready": False, "paper_execution_enabled": False,
}
NORMAL_KNOTS = np.r_[0., ndtri((1 + np.array(SPEC["coverages"])) / 2)]
QUAD_X, QUAD_W = np.polynomial.legendre.leggauss(32)


def implementation_hash():
    return digest({"source": Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n"),
                   "parent": residual.implementation_hash(),
                   "panel": Path(__file__).with_name("weather_model_diagnostics.py").read_text(encoding="utf-8").replace("\r\n", "\n")})


def cutoff(target):
    return (date.fromisoformat(target) - timedelta(days=SPEC["assumed_label_lag_days"])).isoformat()


def fit_shape(errors, target, stations):
    selected = adaptive.window(errors, cutoff(target), SPEC["shape_window_days"])
    if len({(r["station"], r["target_date"]) for r in selected}) != len(selected):
        raise ValueError("DUPLICATE_SHAPE_EVENT")
    if any(r["label_target_cutoff"] > cutoff(r["target_date"]) or not math.isfinite(r["z"]) for r in selected):
        raise ValueError("SHAPE_ERROR_LEAKAGE_OR_NONFINITE")
    pooled = np.quantile([abs(r["z"]) for r in selected], SPEC["coverages"]) if selected else None
    fitted = {}
    for station in stations:
        own = [abs(r["z"]) for r in selected if r["station"] == station]
        reason = "earlier_error_quantiles" if len(own) >= SPEC["minimum_shape_station_dates"] else "insufficient_shape_history"
        knots = NORMAL_KNOTS.copy()
        if reason == "earlier_error_quantiles":
            prior = SPEC["shape_pool_prior"]
            q = (len(own) * np.quantile(own, SPEC["coverages"]) + prior * pooled) / (len(own) + prior)
            knots[1:] = np.clip(q, SPEC["shape_scale_bounds"][0] * NORMAL_KNOTS[1:], SPEC["shape_scale_bounds"][1] * NORMAL_KNOTS[1:])
            # Tied empirical quantiles still need a strictly increasing CDF.
            for i in range(1, len(knots)):
                knots[i] = max(knots[i], knots[i - 1] + 1e-6)
        fitted[station] = {"knots": knots.tolist(), "station_dates": len(own), "reason": reason}
    return {"target_date": target, "label_target_cutoff": cutoff(target), "errors": selected,
            "errors_hash": digest(selected), "stations": fitted}


def validate_knots(knots):
    k = np.asarray(knots, dtype=float)
    if k.shape != NORMAL_KNOTS.shape or not np.isfinite(k).all() or k[0] != 0 or np.any(np.diff(k) <= 0):
        raise ValueError("INVALID_SHAPE_KNOTS")
    return k


def transformed_z(z, knots):
    k = validate_knots(knots)
    z = np.asarray(z, dtype=float)
    a = np.abs(z)
    i = np.clip(np.searchsorted(k, a, side="right") - 1, 0, len(k) - 2)
    slope = np.diff(NORMAL_KNOTS)[i] / np.diff(k)[i]
    transformed = NORMAL_KNOTS[i] + (a - k[i]) * slope
    return np.sign(z) * transformed, slope


def cdf(value, mu, sigma, knots):
    if not math.isfinite(mu) or not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    z, _ = transformed_z((np.asarray(value) - mu) / sigma, knots)
    return ndtr(z)


def temperature_scores(observed, mu, sigma, knots):
    k = validate_knots(knots)
    if not all(math.isfinite(x) for x in (observed, mu, sigma)) or sigma <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    z = (observed - mu) / sigma
    t, slope = transformed_z(z, k)
    # Integrate each smooth segment separately, including the step at the outcome.
    bound = max(30., abs(z) + 30.)
    edges = sorted(set([-bound, bound, z, *k, *(-k)]))
    integral = 0.
    for lo, hi in zip(edges, edges[1:]):
        points = (hi + lo) / 2 + (hi - lo) / 2 * QUAD_X
        values = ndtr(transformed_z(points, k)[0]) - (points >= z)
        integral += (hi - lo) / 2 * float(QUAD_W @ values ** 2)
    bounds = {str(round(c * 100)): (mu - q * sigma, mu + q * sigma) for c, q in zip(SPEC["coverages"], k[1:])}
    return {"mae_f": abs(observed - mu), "squared_error_f2": (observed - mu) ** 2,
            "forecast_bias_f": mu - observed, "crps_f": sigma * integral,
            "nll": float(.5 * t ** 2 + math.log(sigma * math.sqrt(2 * math.pi)) - np.log(slope)),
            **residual.interval_scores(observed, bounds)}


def replay(rows):
    """Rebuild prior-only means/errors, never fit the October frozen paper model."""
    if len({(r['station'], r['target_date']) for r in rows}) != len(rows):
        raise ValueError("DUPLICATE_HISTORY_EVENT")
    stations = sorted({r["station"] for r in rows})
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["target_date"]].append(row)
    parent_errors, shape_errors, predictions, models = [], [], [], {}
    for target, today in sorted(by_day.items()):
        base = adaptive.fit_day(rows, parent_errors, target, "high", stations)
        if base is None:
            continue
        if base["uncertainty_ready"]:
            shift = residual.residual_model(parent_errors, target, "high", stations)
            shape = fit_shape(shape_errors, target, stations)
            model = {"base": base, "shift": shift, "shape": shape}
            model_id = digest(model)
            models[model_id] = model
            for row in today:
                station = row["station"]
                mu = row["forecast_high_f"] + base["stations"][station]["bias_f"] + shift["stations"][station]["mean_shift_f"]
                sigma = base["stations"][station]["sigma_f"]
                p = {"station": station, "target_date": target, "model_id": model_id,
                     "mean_f": mu, "sigma_f": sigma, "knots": shape["stations"][station]["knots"],
                     "run_initialization_utc": row["run_initialization_utc"]}
                predictions.append(p)
                shape_errors.append({"station": station, "target_date": target, "label_target_cutoff": cutoff(target),
                                     "model_id": model_id, "z": (row["observed_high_f"] - mu) / sigma})
        for row in today:
            mu = row["forecast_high_f"] + base["stations"][row["station"]]["bias_f"]
            parent_errors.append({"station": row["station"], "target_date": target, "label_target_cutoff": cutoff(target),
                                  "error_f": row["observed_high_f"] - mu})
    return {"models": models, "predictions": predictions}


def temperature_evaluation(rows, evidence):
    test = [r for r in rows if SPEC["temperature_test_start"] <= r["target_date"] <= SPEC["end"]]
    lookup = {(p["station"], p["target_date"]): p for p in evidence["predictions"]}
    scores = {"reference_gaussian": [], "calibrated_shape": []}
    for row in test:
        p = lookup[row["station"], row["target_date"]]
        for name, knots in (("reference_gaussian", NORMAL_KNOTS), ("calibrated_shape", p["knots"])):
            scores[name].append(temperature_scores(row["observed_high_f"], p["mean_f"], p["sigma_f"], knots))
    return {"station_dates": len(test), "dates": sorted({r["target_date"] for r in test}),
            "overall": {name: static.summarize(values) for name, values in scores.items()},
            "by_station": {s: {name: static.summarize([x for r, x in zip(test, values) if r["station"] == s])
                                 for name, values in scores.items()} for s in sorted({r['station'] for r in test})},
            "paired_uncertainty": static.paired_uncertainty(test, scores["calibrated_shape"], scores["reference_gaussian"])}


def probabilities(rows, prediction, knots):
    result = []
    for row in rows:
        c = row["metadata"]["contract"]
        hi = 1. if c["high_f"] is None else float(cdf(c["high_f"], prediction["mean_f"], prediction["sigma_f"], knots))
        lo = 0. if c["low_f"] is None else float(cdf(c["low_f"], prediction["mean_f"], prediction["sigma_f"], knots))
        result.append(hi - lo)
    validate_probability(result)
    return result


def validate_probability(p):
    a = np.asarray(p, dtype=float)
    if a.ndim != 1 or len(a) < 2 or not np.isfinite(a).all() or np.any(a < 0) or np.any(a > 1) or not math.isclose(float(a.sum()), 1., abs_tol=1e-6):
        raise ValueError("INVALID_PROBABILITY_VECTOR")
    return a


def matched_panel(raw, evidence, as_of):
    source, quality = forward_panel(raw, as_of)
    lookup = {(p["station"], p["target_date"]): p for p in evidence["predictions"]}
    result, rejected, seen = [], Counter(), set()
    for v in source:
        try:
            if v["metric"] != "high" or v["lead_days"] != 1:
                raise ValueError("OUTSIDE_DAYTIME_SCOPE")
            p = lookup.get((v["station"], v["target_date"]))
            if p is None:
                raise ValueError("NO_COMPLETED_GFS_HISTORY_MATCH")
            decision = utc(v["decision_at"])
            local = decision.astimezone(ZoneInfo(station_metadata(v["station"])["timezone"]))
            if local.hour not in SPEC["decision_hours_local"] or local.date() + timedelta(days=1) != date.fromisoformat(v["target_date"]):
                raise ValueError("OUTSIDE_LOCAL_DECISION_SLOT")
            start, end = standard_day(v["station"], date.fromisoformat(v["target_date"]))
            for row in v["rows"]:
                m, c = row["metadata"], row["metadata"]["contract"]
                if (c["station"] != v["station"] or c["target_date"] != v["target_date"] or c["metric"] != "high"
                    or c["platform"] != v["venue"] or c["market_id"] != row["outcome"]
                    or utc(c["window_start"]) != start or utc(c["window_end"]) != end
                    or c["authority"] != m["settlement_source"]
                    or c["low_f"] != m["bucket_low_f"] or c["high_f"] != m["bucket_high_f"]
                    or not 0 <= (decision - utc(m["quote_received_at"])).total_seconds() <= 180):
                    raise ValueError("CONTRACT_OR_QUOTE_MISMATCH")
            key = (v["venue"], v["station"], v["target_date"])
            if key in seen:
                raise ValueError("LATER_REPEATED_SNAPSHOT")
            # Only valid complete vectors consume a first-decision slot.
            reference, candidate = probabilities(v["rows"], p, NORMAL_KNOTS), probabilities(v["rows"], p, p["knots"])
            saved = [r["prob"] for r in v["rows"]]
            validate_probability(saved)
            result.append({**v, "reference": reference, "candidate": candidate, "saved_original": saved,
                           "temperature_model_id": p["model_id"], "historical_as_of_verified": False})
            seen.add(key)
        except (KeyError, ValueError, TypeError) as error:
            rejected[str(error)] += 1
    return result, {"source_quality": quality, "rejected": dict(rejected)}


def blend(market, candidate, weight):
    m, p = validate_probability(market), validate_probability(candidate)
    if m.shape != p.shape or weight not in SPEC["weights"]:
        raise ValueError("INVALID_BLEND")
    if weight == 0:
        return m.copy()
    delta = weight * (p - m)
    largest = float(abs(delta).max())
    if largest > SPEC["maximum_probability_change"]:
        delta *= SPEC["maximum_probability_change"] / largest
    return m + delta


def band(probability):
    return min(len(SPEC["market_probability_bins"]) - 2,
               int(np.searchsorted(SPEC["market_probability_bins"], probability, side="right") - 1))


def guarded(v, model):
    p = blend(v["market"], v["candidate"], model["weight"])
    if any(p[i] > m + 1e-12 and band(m) not in model["supported_bands"] for i, m in enumerate(v["market"])):
        return np.asarray(v["market"]), "unsupported_positive_adjustment_band"
    return p, "qualified_adjustment" if model["weight"] else model["reason"]


def event_scores(panel, predict):
    events = defaultdict(list)
    for v in panel:
        p = validate_probability(predict(v))
        truth = np.array([int(r["is_correct"]) for r in v["rows"]])
        if truth.sum() != 1:
            raise ValueError("INVALID_OFFICIAL_OUTCOME")
        events[v["station"], v["target_date"]].append({"brier": float(np.sum((p - truth) ** 2)),
                                                    "log_loss": -math.log(max(1e-12, float(p @ truth)))})
    return {key: {name: mean(s[name] for s in values) for name in ("brier", "log_loss")} for key, values in events.items()}


def aggregate(events):
    return {name: mean(v[name] for v in events.values()) for name in ("brier", "log_loss")} if events else None


def uncertainty(differences):
    """Each input is already one station/date; resample whole calendar blocks."""
    if not differences:
        return {"blocks": 0, "upper_95": None}
    origin = min(date.fromisoformat(k[1]) for k in differences)
    blocks = defaultdict(list)
    for (_, target), value in differences.items():
        blocks[(date.fromisoformat(target) - origin).days // SPEC["bootstrap_calendar_days"]].append(value)
    values = list(blocks.values())
    if len(values) < 3:
        return {"blocks": len(values), "upper_95": None}
    rng = np.random.default_rng(SPEC["seed"])
    sums, counts = np.array([sum(v) for v in values]), np.array([len(v) for v in values])
    draws = rng.integers(len(values), size=(SPEC["bootstrap_samples"], len(values)))
    estimates = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    return {"blocks": len(values), "mean": mean(differences.values()),
            "interval_95": np.quantile(estimates, [.025, .975]).tolist(), "upper_95": float(np.quantile(estimates, .975))}


def fit_guard(panel, before):
    # Raw panels can contain later outcomes; they must never enter selection.
    calibration = [v for v in panel if SPEC["market_cal_start"] <= v["target_date"] <= SPEC["market_cal_end"]
                   and utc(v["decision_at"]) < utc(before)
                   and all(utc(r['resolved_at']) < utc(before) and utc(r['metadata']['label_available_at']) < utc(before) for r in v['rows'])]
    dates = sorted({v["target_date"] for v in calibration})
    baseline = event_scores(calibration, lambda v: v["market"])
    candidates = []
    enough = len(dates) >= SPEC["minimum_guard_dates"] and len(baseline) >= SPEC["minimum_guard_station_dates"]
    if enough:
        for weight in SPEC["weights"][1:]:
            bands = defaultdict(lambda: defaultdict(list))
            for v in calibration:
                p = blend(v["market"], v["candidate"], weight)
                for i, m in enumerate(v["market"]):
                    if p[i] > m + 1e-12:
                        y = int(v["rows"][i]["is_correct"])
                        bands[band(m)][v['station'], v['target_date']].append(float((p[i] - y) ** 2 - (m - y) ** 2))
            support, details = [], {}
            for b, events in bands.items():
                diffs = {k: mean(v) for k, v in events.items()}
                interval = uncertainty(diffs)
                passed = (len(diffs) >= SPEC["minimum_band_station_dates"] and len({k[1] for k in diffs}) >= SPEC["minimum_guard_dates"]
                          and interval["upper_95"] is not None and interval["upper_95"] < -SPEC["minimum_gain"])
                if passed:
                    support.append(b)
                details[str(b)] = {"station_dates": len(diffs), "uncertainty": interval, "passed": passed}
            candidate = {"weight": weight, "supported_bands": sorted(support), "reason": "calibration"}
            events = event_scores(calibration, lambda v: guarded(v, candidate)[0])
            intervals = {name: uncertainty({k: s[name] - baseline[k][name] for k, s in events.items()}) for name in ("brier", "log_loss")}
            by_date = {day: [k for k in events if k[1] == day] for day in dates}
            fraction = mean(all(mean(events[k][name] - baseline[k][name] for k in keys) < 0 for name in ("brier", "log_loss")) for keys in by_date.values())
            passed = bool(support) and fraction >= SPEC["minimum_fraction_dates_improved"] and all(i["upper_95"] is not None and i["upper_95"] < -SPEC["minimum_gain"] for i in intervals.values())
            candidates.append({**candidate, "scores": aggregate(events), "bands": details, "uncertainty": intervals,
                               "fraction_dates_improved": fraction, "passed": passed})
    passed = [c for c in candidates if c["passed"]]
    selected = min(passed, key=lambda c: (c["scores"]["log_loss"], c["weight"])) if passed else None
    return {"weight": selected["weight"] if selected else 0., "supported_bands": selected["supported_bands"] if selected else [],
            "reason": "qualified_calibration_gain" if selected else "no_qualified_gain" if enough else "insufficient_calibration",
            "fit_before": utc(before).isoformat(), "dates": dates, "station_dates": len(baseline),
            "calibration_hash": digest(calibration), "market_scores": aggregate(baseline), "candidates": candidates,
            "latest_label_at": max((r['resolved_at'] for v in calibration for r in v['rows']), key=utc, default=None),
            "selection_used_test_outcomes": False}


def quote_diagnostics(panel, predict):
    """One hypothetical contract per event/venue; never aggregate venue bankrolls."""
    output = {}
    for venue in sorted({v['venue'] for v in panel}):
        scenarios = {}
        for extra in SPEC['extra_slippage']:
            records, missing = [], 0
            for v in panel:
                if v['venue'] != venue:
                    continue
                candidates = []
                for i, (p, row) in enumerate(zip(predict(v), v['rows'])):
                    m = row['metadata']; cost, depth = m.get('executable_cost'), m.get('ask_depth')
                    fee = m.get('fee_estimate') or {}
                    if (not isinstance(cost, (int, float)) or not math.isfinite(cost) or not 0 < cost < 1
                        or not isinstance(depth, (int, float)) or not math.isfinite(depth) or depth < 1 or not fee.get('fee_model_version')):
                        missing += 1
                        continue
                    if p - cost - extra >= SPEC['minimum_edge_after_cost']:
                        candidates.append((float(p - cost - extra), -i, i, cost + extra))
                if candidates:
                    _, _, i, cost = max(candidates)
                    records.append({'station': v['station'], 'target_date': v['target_date'], 'probability': float(predict(v)[i]),
                                    'cost': cost, 'outcome': int(v['rows'][i]['is_correct'])})
            scenarios[str(extra)] = {'contracts': len(records), 'station_dates': len({(r['station'], r['target_date']) for r in records}),
                                    'hypothetical_unit_pnl': sum(r['outcome'] - r['cost'] for r in records),
                                    'missing_cost_evidence_buckets': missing, 'positions': records}
        output[venue] = scenarios
    return {'by_venue': output, 'actual_fills_verified': False, 'bankroll_or_risk_simulation': False,
            'interpretation': 'Quote-only sensitivity; this is neither the frozen paper book nor realized profit.'}


def market_evaluation(panel):
    test = [v for v in panel if SPEC['market_test_start'] <= v['target_date'] <= SPEC['market_test_end']]
    if not test:
        return {'status': 'no_matching_market_test', 'guard': None}
    guard = fit_guard(panel, min(utc(v['decision_at']) for v in test))
    methods = {**{name: lambda v, n=name: v[n] for name in ('market', 'saved_original', 'reference', 'candidate')},
               'guarded': lambda v: guarded(v, guard)[0]}
    events = {name: event_scores(test, f) for name, f in methods.items()}
    return {'status': 'retrospective_only_market_fallback' if guard['weight'] == 0 else 'retrospective_only_requires_future_validation',
            'guard': guard, 'test_dates': sorted({v['target_date'] for v in test}), 'test_station_dates': len(events['market']),
            'test_vectors': len(test), 'scores': {name: aggregate(values) for name, values in events.items()},
            'by_venue': {venue: {name: aggregate(event_scores([v for v in test if v['venue'] == venue], f)) for name, f in methods.items()}
                         for venue in sorted({v['venue'] for v in test})},
            'paired_vs_market': {name: {metric: uncertainty({k: values[metric] - events['market'][k][metric] for k, values in result.items()})
                                       for metric in ('brier', 'log_loss')} for name, result in events.items() if name != 'market'},
            'guard_reasons': dict(Counter(guarded(v, guard)[1] for v in test)),
            'cost_sensitivity': {name: quote_diagnostics(test, methods[name]) for name in ('reference', 'candidate', 'guarded')}}


def verify_artifact(artifact, identity):
    if artifact.get('identity') != identity or digest({k: v for k, v in artifact.items() if k != 'artifact_id'}) != artifact.get('artifact_id'):
        raise ValueError('CHALLENGER_ARTIFACT_IDENTITY_MISMATCH')
    if any(artifact.get(k) is not False for k in ('historical_as_of_verified', 'live_ready', 'paper_execution_enabled')):
        raise ValueError('CHALLENGER_ELIGIBILITY_MISMATCH')
    for key, m in artifact['models'].items():
        target = m['base']['target_date']
        if digest(m) != key or any(m[n]['target_date'] != target or m[n]['label_target_cutoff'] != cutoff(target) for n in ('base', 'shift', 'shape')):
            raise ValueError('CHALLENGER_MODEL_LINEAGE_MISMATCH')
        shape = m['shape']
        if shape['errors_hash'] != digest(shape['errors']) or any(r['target_date'] > cutoff(target) or r['label_target_cutoff'] > cutoff(r['target_date']) for r in shape['errors']):
            raise ValueError('CHALLENGER_SHAPE_LEAKAGE')
        for value in shape['stations'].values():
            validate_knots(value['knots'])
    for p in artifact['predictions']:
        m = artifact['models'].get(p['model_id'])
        if not m or m['base']['target_date'] != p['target_date'] or p['knots'] != m['shape']['stations'][p['station']]['knots']:
            raise ValueError('CHALLENGER_PREDICTION_LINEAGE_MISMATCH')


def run(history, market_path, output):
    history, output = Path(history), Path(output)
    write_once(output / 'spec.json', SPEC)
    raw, status_bytes, market_bytes = (history / 'dataset.json').read_bytes(), (history / 'latest.json').read_bytes(), Path(market_path).read_bytes()
    status = json.loads(status_bytes)
    if status.get('status') not in ('complete', 'complete_with_gaps') or status.get('model') != 'gfs_global':
        raise ValueError('COMPLETED_GFS_HISTORY_REQUIRED')
    identity = {'spec_hash': digest(SPEC), 'implementation_hash': implementation_hash(),
                'input_sha256': hashlib.sha256(raw).hexdigest(), 'status_sha256': hashlib.sha256(status_bytes).hexdigest(),
                'market_input_sha256': hashlib.sha256(market_bytes).hexdigest(), 'numpy': np.__version__, 'scipy': scipy.__version__}
    run_id = digest(identity); report_path = output / (run_id + '.json')
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding='utf-8'))
        if report.get('identity') != identity or report.get('report_hash') != digest({k: v for k, v in report.items() if k != 'report_hash'}):
            raise ValueError('EXISTING_CHALLENGER_REPORT_MISMATCH')
        artifact = json.loads((output / (report['artifact_id'] + '_replay.json')).read_text(encoding='utf-8'))
        verify_artifact(artifact, identity)
        if artifact['artifact_id'] != report['artifact_id']:
            raise ValueError('CHALLENGER_REPORT_LINK_MISMATCH')
        return report
    now = datetime.now(timezone.utc).isoformat()
    rows = static.validate(json.loads(raw), status['dataset_hash'], now)
    rows = [r for r in rows if SPEC['start'] <= r['target_date'] <= SPEC['end']]
    static.split(rows)  # Existing all-21-station coverage requirements.
    artifact = {**replay(rows), 'identity': identity, 'created_at': now, 'mode': 'offline_research',
                'historical_as_of_verified': False, 'live_ready': False, 'paper_execution_enabled': False}
    market = json.loads(market_bytes)
    panel, quality = matched_panel(market['v3'] if isinstance(market, dict) else market, artifact, now)
    artifact['market_panel'] = panel
    artifact['artifact_id'] = digest(artifact)
    verify_artifact(artifact, identity)
    write_once(output / (artifact['artifact_id'] + '_replay.json'), artifact)
    report = {'run_id': run_id, 'identity': identity, 'artifact_id': artifact['artifact_id'], 'created_at': now,
              'status': 'separate_challenger_tested_no_promotion', 'spec': SPEC,
              'coverage': {'source_station_dates': len(rows), 'stations': sorted({r['station'] for r in rows}),
                           'source_dates': len({r['target_date'] for r in rows}), 'market_quality': quality},
              'temperature': temperature_evaluation(rows, artifact), 'market': market_evaluation(panel),
              'live_ready': False, 'paper_execution_enabled': False, 'profitability_proven': False,
              'frozen_daytime_changed': False, 'hosted_backfill_changed': False,
              'limitations': ['Previously inspected months; exploratory research, not an unseen holdout.',
                              'Historical forecast publication and CLI label timing are unverified; three-day temperature label lag is assumed.',
                              'Earlier evaluation labels may enter later temperature fits; market guard is frozen before its test decisions.',
                              'A probability adjustment is not proven by matching the market. Few date blocks limit uncertainty estimates.',
                              'No retrospective predictions are written as prospective decisions. Quote sensitivity is not realized profit.']}
    report['report_hash'] = digest(report)
    write_once(report_path, report)
    return report
