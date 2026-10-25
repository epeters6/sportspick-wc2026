"""Separate offline model/lead calibration. No collectors or deployed-model writes."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.special import ndtr, ndtri

from backend import weather_archive_retest as archive
from backend import weather_verified_runs as runs
from backend.ml.weather_learning.dataset import digest
from backend.weather_backfill_checkpoint import PLAN_ID
from backend.weather_probability_screen import write_once

SPEC = {
    "version": "weather_model_lead_calibration_v1", "plan_id": PLAN_ID,
    "models": list(archive.MODELS), "metrics": ["high", "low"],
    "stations": archive.SPEC["stations"],
    "train_start": "2026-04-03", "train_end": "2026-08-27",
    "calibration_start": "2026-08-31", "calibration_end": "2026-09-16",
    "test_start": "2026-09-20", "test_end": "2026-10-19",
    "lead_definition": "hours from exact run initialization to target standard-day start; not run age",
    "lead_boundaries_hours": [18, 30, 42], "station_prior_events": 30,
    "minimum_station_events": 14, "minimum_pool_events": 60, "minimum_pool_dates": 7,
    "sigma_floor_f": 0.75,
    "mean": "station residual mean shrunk to same model/lead pool, trained on earlier months",
    "width": "later calibration RMS residual, shrunk within same model/lead; no cross-model backoff",
    "primary": "equal mixture of three separately calibrated Gaussian distributions",
    "secondary": "each model separately; no best-model or best-station selection",
    "revisions": "record exact six-hour changes; do not fit a revision coefficient in this version",
    "historical_as_of_verified": False, "mode": "offline_research",
    "live_ready": False, "paper_execution_enabled": False,
}
EVALUATION = {
    "version": "weather_fresh_run_evaluation_v1", "first_target": "2026-10-27",
    "last_target": "2026-11-05", "stations": SPEC["stations"],
    "primary_arm": "fresh_gfs_ablation", "baseline": "market",
    "secondary_arm": "frozen_recipe_at_shadow_time",
    "minimum_scoring_dates": 7, "minimum_station_dates": 14,
    "bootstrap_samples": 2000, "calendar_block_days": 3, "seed": 260,
    "minimum_quote_edge": 0.05, "extra_slippage": [0., 0.01, 0.03],
    "reliability_bins": [0., 0.05, 0.10, 0.20, 0.35, 0.50, 1.],
    "revision_split_f": 2., "candidate_disagreement_limit": 0.05,
    "decision": "diagnostic only; no automated selection, promotion, or profitability claim",
    "new_calibrated_model": "retrospective replay only; not saved forward evidence",
    "paper_execution_enabled": False, "live_ready": False,
}


def implementation_hash():
    paths = [Path(__file__).with_name(name) for name in (
        "weather_model_lead_calibration.py", "weather_fresh_run_evaluation.py",
        "weather_archive_retest.py", "weather_verified_runs.py", "weather_verified_shadow.py",
        "weather_daytime_paper.py")]
    return digest({p.name: p.read_text(encoding="utf-8").replace("\r\n", "\n") for p in paths})


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def signed(value, field):
    return {**value, field: digest({k: v for k, v in value.items() if k != field})}


def verify(value, field):
    if value.get(field) != digest({k: v for k, v in value.items() if k != field}):
        raise ValueError("CALIBRATION_EVIDENCE_HASH_MISMATCH")


def freeze(output, now=None):
    path = Path(output)/"protocol.json"
    if path.exists():
        protocol = read(path)
        verify_protocol(protocol)
        return protocol
    now = now or datetime.now(timezone.utc)
    if runs.timestamp(now) >= runs.timestamp(EVALUATION["first_target"]+"T00:00Z") - timedelta(days=1):
        raise ValueError("EVALUATION_MUST_BE_FROZEN_BEFORE_FIRST_DECISION_DAY")
    protocol = signed({"created_at": now.isoformat(), "calibration": SPEC, "evaluation": EVALUATION,
                       "implementation_hash": implementation_hash()}, "protocol_id")
    write_once(path, protocol)
    return protocol


def verify_protocol(protocol):
    verify(protocol, "protocol_id")
    if (protocol["calibration"] != SPEC or protocol["evaluation"] != EVALUATION
            or protocol["implementation_hash"] != implementation_hash()):
        raise ValueError("CALIBRATION_PROTOCOL_CHANGED")
    if runs.timestamp(protocol["created_at"]) >= runs.timestamp(EVALUATION["first_target"]+"T00:00Z") - timedelta(days=1):
        raise ValueError("EVALUATION_LATE_FREEZE")


def lead_band(initialization, start):
    lead = (runs.timestamp(start)-runs.timestamp(initialization)).total_seconds()/3600
    if not 0 < lead <= 72:
        raise ValueError("INVALID_FORECAST_LEAD")
    return sum(lead >= boundary for boundary in SPEC["lead_boundaries_hours"])


def records(rows, model, metric):
    """An identical run seen at several local slots is one fitting observation."""
    unique = {}
    for row in rows:
        f = row["forecasts"][model]["current"]
        value = {"station": row["station"], "target_date": row["target_date"],
                 "run": f["run_initialization_utc"], "raw": f[metric+"_f"],
                 "observed": row["observed_"+metric+"_f"],
                 "band": lead_band(f["run_initialization_utc"], f["standard_day_start"])}
        if not all(runs.finite(value[k]) for k in ("raw", "observed")):
            raise ValueError("NONFINITE_CALIBRATION_TEMPERATURE")
        key = value["station"], value["target_date"], value["run"]
        if key in unique and unique[key] != value:
            raise ValueError("CONFLICTING_IDENTICAL_FORECAST_RUN")
        unique[key] = value
    return list(unique.values())


def coverage(rows):
    return {"rows": len(rows), "station_dates": len({(r['station'], r['target_date']) for r in rows}),
            "dates": len({r['target_date'] for r in rows}), "stations": len({r['station'] for r in rows})}


def supported(rows):
    c = coverage(rows)
    return c["station_dates"] >= SPEC["minimum_pool_events"] and c["dates"] >= SPEC["minimum_pool_dates"]


def average(rows, values):
    return float(np.average(values, weights=archive.row_weights(rows)))


def fit_cells(train, calibration, model, metric):
    tr, cal = records(train, model, metric), records(calibration, model, metric)
    cells = {}
    for band in range(len(SPEC["lead_boundaries_hours"])+1):
        t, c = [r for r in tr if r["band"] == band], [r for r in cal if r["band"] == band]
        cell = {"train_coverage": coverage(t), "calibration_coverage": coverage(c), "supported": False}
        cells[str(band)] = cell
        if not supported(t) or not supported(c):
            cell["reason"] = "INSUFFICIENT_SAME_MODEL_LEAD_EVIDENCE"
            continue
        pool_bias = average(t, [r["observed"]-r["raw"] for r in t])
        biases = {}
        for station in SPEC["stations"]:
            own = [r for r in t if r["station"] == station]
            n = coverage(own)["station_dates"]
            own_mean = average(own, [r["observed"]-r["raw"] for r in own]) if own else pool_bias
            shrink = n/(n+SPEC["station_prior_events"]) if n >= SPEC["minimum_station_events"] else 0.
            biases[station] = pool_bias + shrink*(own_mean-pool_bias)
        errors = [r["observed"]-r["raw"]-biases[r["station"]] for r in c]
        pool_mse = average(c, np.square(errors))
        stations = {}
        for station in SPEC["stations"]:
            own = [r for r in c if r["station"] == station]
            n = coverage(own)["station_dates"]
            own_mse = average(own, [(r["observed"]-r["raw"]-biases[station])**2 for r in own]) if own else pool_mse
            shrink = n/(n+SPEC["station_prior_events"]) if n >= SPEC["minimum_station_events"] else 0.
            stations[station] = {"bias_f": biases[station],
                "sigma_f": max(SPEC["sigma_floor_f"], math.sqrt(pool_mse+shrink*(own_mse-pool_mse))),
                "calibration_station_dates": n}
        cell.update(supported=True, pool_bias_f=pool_bias, pool_mse_f2=pool_mse, stations=stations)
    return cells


def fit(rows):
    train = [r for r in rows if SPEC["train_start"] <= r["target_date"] <= SPEC["train_end"]]
    cal = [r for r in rows if SPEC["calibration_start"] <= r["target_date"] <= SPEC["calibration_end"]]
    test = [r for r in rows if SPEC["test_start"] <= r["target_date"] <= SPEC["test_end"]]
    for part, minimum in ((train, 90), (cal, 10), (test, 20)):
        if coverage(part)["dates"] < minimum or set(r['station'] for r in part) != set(SPEC['stations']):
            raise ValueError("INSUFFICIENT_ALL_STATION_PARTITION_COVERAGE")
    model = {"spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
             "train_hash": digest(train), "calibration_hash": digest(cal),
             "label_target_cutoff": SPEC["calibration_end"], "historical_as_of_verified": False,
             "mode": "offline_research", "live_ready": False, "paper_execution_enabled": False,
             "cells": {metric: {m: fit_cells(train, cal, m, metric) for m in SPEC['models']} for metric in SPEC['metrics']}}
    if any(not any(cell['supported'] for cell in cells.values())
           for models in model['cells'].values() for cells in models.values()):
        raise ValueError('NO_SUPPORTED_MODEL_LEAD_CALIBRATION')
    model = signed(model, "model_id")
    return model, test, {name: coverage(part) for name, part in (("train", train), ("calibration", cal), ("test", test))}


def distribution(model, name, metric, station, forecast, initialization, start):
    band = str(lead_band(initialization, start))
    cell = model["cells"][metric][name][band]
    if not cell["supported"]:
        return None
    value = cell["stations"][station]
    return {"mean_f": forecast+value["bias_f"], "sigma_f": value["sigma_f"], "model": name, "band": band}


def normal_crps(y, mu, sigma):
    z = (y-mu)/sigma
    return sigma*(z*(2*ndtr(z)-1)+2*math.exp(-z*z/2)/math.sqrt(2*math.pi)-1/math.sqrt(math.pi))


def mixture_crps(y, components):
    def absolute_normal(mu, sigma):
        z = mu/sigma
        return 2*sigma*math.exp(-z*z/2)/math.sqrt(2*math.pi)+mu*(2*ndtr(z)-1)
    first = np.mean([absolute_normal(c['mean_f']-y, c['sigma_f']) for c in components])
    second = np.mean([absolute_normal(a['mean_f']-b['mean_f'], math.hypot(a['sigma_f'], b['sigma_f']))
                      for a in components for b in components])
    return float(first-.5*second)


@lru_cache(maxsize=4096)
def interval_bounds(parameters, level):
    components = [{'mean_f':mu, 'sigma_f':sigma} for mu,sigma in parameters]
    def quantile(p):
        lower = min(c['mean_f']-10*c['sigma_f'] for c in components)
        upper = max(c['mean_f']+10*c['sigma_f'] for c in components)
        for _ in range(45):
            middle = (lower+upper)/2
            if np.mean([ndtr((middle-c['mean_f'])/c['sigma_f']) for c in components]) < p:
                lower = middle
            else:
                upper = middle
        return (lower+upper)/2
    tail = (1-level)/2
    if len(components) == 1:
        c = components[0]; width = float(ndtri(1-tail))*c['sigma_f']
        return c['mean_f']-width, c['mean_f']+width
    return quantile(tail), quantile(1-tail)


def interval_scores(y, components):
    result = {}
    for level in (.5,.8,.9,.95):
        low, high = interval_bounds(tuple((c['mean_f'], c['sigma_f']) for c in components),level)
        suffix = str(round(level*100))
        result['coverage_'+suffix] = float(low <= y <= high)
        result['width_'+suffix+'_f'] = high-low
    return result


def weather_evaluation(test, model):
    result = {}
    for metric in SPEC['metrics']:
        scores, paired, missing = defaultdict(list), [], Counter()
        for row in test:
            components = {}
            for name in SPEC['models']:
                f = row['forecasts'][name]['current']
                components[name] = distribution(model, name, metric, row['station'], f[metric+'_f'],
                                                 f['run_initialization_utc'], f['standard_day_start'])
            if any(c is None for c in components.values()):
                for name, c in components.items():
                    if c is None: missing[name] += 1
                continue
            paired.append(row)
            y = row['observed_'+metric+'_f']
            for name, c in components.items():
                scores[name].append({'mae_f': abs(c['mean_f']-y), 'bias_f': c['mean_f']-y,
                                    'crps_f': normal_crps(y, c['mean_f'], c['sigma_f']), **interval_scores(y,[c])})
                raw = row['forecasts'][name]['current'][metric+'_f']
                scores['raw_'+name].append({'mae_f': abs(raw-y), 'bias_f': raw-y})
            values = list(components.values())
            mu = np.mean([c['mean_f'] for c in values])
            scores['calibrated_mixture'].append({'mae_f': abs(mu-y), 'bias_f': mu-y,
                                                  'crps_f': mixture_crps(y, values), **interval_scores(y,values)})
        result[metric] = {'paired_coverage': coverage(paired), 'unsupported_model_lead_rows': dict(missing),
                         'scores': {name: {k: average(paired, [float(v[k]) for v in values]) for k in values[0]}
                                    for name, values in scores.items()}}
    return result


def complete_input(directory, completion_directory):
    """Read a final, already verified download; never read or repair a quota ledger."""
    directory, completion_directory = Path(directory), Path(completion_directory)
    marker = completion_directory/'completion.json'
    if not marker.exists():
        return None
    done = read(marker)
    if done['report_file'] != done['report_hash']+'_report.json':
        raise ValueError('COMPLETION_PATH_MISMATCH')
    report = read(completion_directory/done['report_file'])
    if digest(report) != done['report_hash'] or report['status'] != 'offline_retest_complete':
        raise ValueError('COMPLETION_REPORT_MISMATCH')
    plan, status = read(directory/'plan.json'), read(directory/'latest.json')
    if (plan.get('plan_id') != PLAN_ID or digest({k:v for k,v in plan.items() if k!='plan_id'}) != PLAN_ID
            or status['plan_id'] != PLAN_ID or status['status'] not in ('complete', 'complete_with_gaps')
            or status['requests_available'] != len(plan['requests'])
            or status['dataset_hash'] != done['dataset_hash'] or status['dataset_hash'] != report['dataset_hash']):
        raise ValueError('INCOMPLETE_OR_MISMATCHED_FINAL_ARCHIVE')
    if status['dataset_file'] != status['dataset_hash']+'_dataset.json':
        raise ValueError('DATASET_PATH_MISMATCH')
    rows = read(directory/status['dataset_file'])
    if digest(rows) != status['dataset_hash']:
        raise ValueError('DATASET_HASH_MISMATCH')
    accepted, rejected = archive.validated_rows(rows, plan)
    # Old validator predates some stricter extrema checks; refuse corrupt values.
    labels = {}
    for row in accepted:
        label_key = row['station'], row['target_date']
        label_value = row['observed_high_f'], row['observed_low_f'], row['label_receipt_hash']
        if label_key in labels and labels[label_key] != label_value:
            raise ValueError('CONFLICTING_STATION_DATE_LABEL')
        labels[label_key] = label_value
        for name in SPEC['models']:
            f = row['forecasts'][name]['current']
            if f['low_f'] > f['high_f']:
                raise ValueError('FORECAST_EXTREMA_REVERSED')
    return accepted, rejected, status['dataset_hash']


def run_fit(directory, completion_directory, output):
    output = Path(output)
    protocol = read(output/'protocol.json'); verify_protocol(protocol)
    loaded = complete_input(directory, completion_directory)
    if loaded is None:
        return {'status': 'awaiting_verified_complete_archive', 'fitted': False,
                'protocol_id': protocol['protocol_id'], 'downloads': 0, 'production_model_changed': False}
    rows, rejected, dataset_hash = loaded
    identity = digest({'protocol_id': protocol['protocol_id'], 'dataset_hash': dataset_hash})
    path = output/(identity+'_fit.json')
    if path.exists():
        saved = read(path); verify(saved, 'evidence_hash'); verify(saved['model'], 'model_id')
        if saved['identity'] != identity or saved['dataset_hash'] != dataset_hash:
            raise ValueError('FIT_IDENTITY_MISMATCH')
        return saved
    model, test, partitions = fit(rows)
    result = signed({'identity': identity, 'protocol_id': protocol['protocol_id'], 'dataset_hash': dataset_hash,
        'status': 'offline_calibration_complete', 'created_at': datetime.now(timezone.utc).isoformat(),
        'model': model, 'coverage': partitions, 'rejected': rejected, 'weather_test': weather_evaluation(test, model),
        'live_ready': False, 'paper_execution_enabled': False, 'profitability_proven': False,
        'limitations': ['Previously inspected months; exploratory, not an untouched holdout.',
            'Synthetic archive slots do not establish historical publication or label availability.',
            'All-model matched rows only; missing evidence and unsupported model/lead cells are reported.',
            'Hourly extrema may differ from official daily extrema.', 'No model deployment or forward predictions.']}, 'evidence_hash')
    write_once(path, result)
    return result
