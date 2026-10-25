"""Offline correction reliability and condition/age uncertainty challenger.

Reads completed GFS evidence only. Never writes a deployed model or forward
decision, and never opens the in-progress hosted archive or its quota ledger.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import scipy

from backend import weather_daytime_challenger as parent
from backend import weather_adaptive_calibration as rolling
from backend import weather_station_distribution as static
from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_probability_screen import write_once

SPEC = {
    'version': 'weather_reliability_challenger_v1', 'metric': 'high',
    'start': '2026-04-03', 'end': '2026-10-18', 'test_start': '2026-09-20',
    'stations': list(parent.SPEC['stations']), 'assumed_label_lag_days': 3,
    'reliability_window_days': 60, 'minimum_station_dates': 21,
    'correction_weights': [0., .25, .5, .75, 1.], 'risk_pool_prior': 30.,
    'minimum_mae_gain_f': .01, 'bootstrap_block_days': 7,
    'bootstrap_samples': 500, 'minimum_blocks': 3, 'seed': 251,
    'reliability': 'Earlier rolling-reference errors; station MAE upper bound and MSE must improve versus raw; pooled risk ranks eligible fixed weights',
    'shape_window_days': 60, 'shape_pool_prior': 30.,
    'range_boundaries_f': [10., 20.], 'age_boundaries_hours': [12., 24.],
    'minimum_context_events': 60, 'minimum_context_dates': 7,
    'shape': 'PR23 monotone quantile warp fitted to errors of earlier reliability-adjusted predictions, with range and verified-age backoff',
    'age_policy': 'Only actual verified receipt/decision timestamps; missing age remains unknown, never imputed from a local slot',
    'evaluation': 'Previously inspected months, exploratory rolling replay; not an unseen holdout',
    'market_guard': 'Unchanged PR23 earlier-label support and bounded adjustment rules',
    'prospective_evaluation': 'New challenger remains offline; October 23-24 inspected outcomes are excluded; no automatic promotion',
    'historical_as_of_verified': False, 'live_ready': False, 'paper_execution_enabled': False,
}
VARIANTS = ('raw_mean_same_width', 'reference_gaussian', 'pr23',
            'reliability_same_shape', 'reliability_global_shape', 'reliability_context_shape')


def implementation_hash():
    return digest({'source': Path(__file__).read_text(encoding='utf-8').replace('\r\n', '\n'),
                   'parent': parent.implementation_hash()})


def context(row):
    """Outcome-free covariates. Age needs observed timing, not an archive run ID."""
    high, low = float(row['forecast_high_f']), float(row['forecast_low_f'])
    if not math.isfinite(high) or not math.isfinite(low) or low > high:
        raise ValueError('INVALID_FORECAST_RANGE')
    result = {'range_band': sum(high-low >= b for b in SPEC['range_boundaries_f']),
              'age_band': None, 'run_age_hours': None, 'age_status': 'unknown_archive_decision_time'}
    timing = ('decision_at', 'forecast_received_at')
    if any(row.get(k) is not None for k in timing) or row.get('forecast_timing_verified'):
        if (not all(row.get(k) for k in (*timing, 'run_initialization_utc'))
                or row.get('forecast_timing_verified') is not True):
            raise ValueError('UNVERIFIED_AGE_EVIDENCE')
        decision, received, run = (utc(row[k]) for k in (*timing, 'run_initialization_utc'))
        if not run <= received <= decision:
            raise ValueError('FORECAST_TIMING_ORDER')
        age = (decision-run).total_seconds()/3600
        result.update(age_band=sum(age >= b for b in SPEC['age_boundaries_hours']),
                      run_age_hours=age, age_status='verified_receipt_before_decision')
    return result


def earlier(rows, target, window_days):
    selected = rolling.window(rows, parent.cutoff(target), window_days)
    if len({(r['station'], r['target_date']) for r in selected}) != len(selected):
        raise ValueError('DUPLICATE_CALIBRATION_EVENT')
    if any(r['label_target_cutoff'] != parent.cutoff(r['target_date']) for r in selected):
        raise ValueError('CALIBRATION_LABEL_LEAKAGE')
    return selected


def gain_interval(rows, values):
    grouped = defaultdict(list)
    origin = min((utc(r['target_date']+'T00:00:00Z') for r in rows), default=None)
    for r, value in zip(rows, values):
        grouped[(utc(r['target_date']+'T00:00:00Z')-origin).days // SPEC['bootstrap_block_days']].append(value)
    blocks = list(grouped.values())
    if len(blocks) < SPEC['minimum_blocks']:
        return {'blocks': len(blocks), 'upper_95': None}
    rng = np.random.default_rng(SPEC['seed'])
    draws = rng.integers(len(blocks), size=(SPEC['bootstrap_samples'], len(blocks)))
    sums, counts = np.array([sum(b) for b in blocks]), np.array([len(b) for b in blocks])
    estimates = sums[draws].sum(axis=1)/counts[draws].sum(axis=1)
    return {'blocks': len(blocks), 'upper_95': float(np.quantile(estimates, .975))}


def fit_reliability(errors, target, stations):
    selected = earlier(errors, target, SPEC['reliability_window_days'])
    if any(not all(math.isfinite(r[k]) for k in ('raw_error_f', 'correction_f')) for r in selected):
        raise ValueError('INVALID_RELIABILITY_ERROR')
    pool = np.array([r['raw_error_f'] for r in selected])
    correction = np.array([r['correction_f'] for r in selected])
    fitted = {}
    for station in stations:
        own = [r for r in selected if r['station'] == station]
        y, c = np.array([r['raw_error_f'] for r in own]), np.array([r['correction_f'] for r in own])
        trials = []
        if len(own) >= SPEC['minimum_station_dates']:
            for weight in SPEC['correction_weights'][1:]:
                error = y-weight*c
                delta = abs(error)-abs(y)
                interval = gain_interval(own, delta)
                mse_delta = float(np.mean(error**2-y**2))
                prior = SPEC['risk_pool_prior']
                risk = float((abs(error).sum()+prior*np.mean(abs(pool-weight*correction)))/(len(own)+prior))
                passed = interval['upper_95'] is not None and interval['upper_95'] < -SPEC['minimum_mae_gain_f'] and mse_delta <= 0
                trials.append({'weight': weight, 'station_mae_delta_f': float(np.mean(delta)),
                               'station_mse_delta_f2': mse_delta, 'pooled_regularized_mae_f': risk,
                               'interval': interval, 'passed': bool(passed)})
        passed = [t for t in trials if t['passed']]
        best = min(passed, key=lambda t: (t['pooled_regularized_mae_f'], t['weight'])) if passed else None
        fitted[station] = {'weight': best['weight'] if best else 0., 'station_dates': len(own),
                           'reason': 'earlier_correction_gain' if best else 'raw_forecast_fallback', 'trials': trials}
    return {'target_date': target, 'label_target_cutoff': parent.cutoff(target),
            'errors_hash': digest(selected), 'latest_error_target': max((r['target_date'] for r in selected), default=None),
            'stations': fitted}


def supported(rows):
    return len(rows) >= SPEC['minimum_context_events'] and len({r['target_date'] for r in rows}) >= SPEC['minimum_context_dates']


def fit_context_shape(errors, target, station, query):
    selected = earlier(errors, target, SPEC['shape_window_days'])
    if any(not math.isfinite(r['z']) or r['range_band'] not in (0, 1, 2)
           or r['age_band'] not in (None, 0, 1, 2) for r in selected):
        raise ValueError('INVALID_SHAPE_CONTEXT')
    if query['range_band'] not in (0, 1, 2) or query['age_band'] not in (None, 0, 1, 2):
        raise ValueError('INVALID_QUERY_CONTEXT')
    pool = [r for r in selected if r['range_band'] == query['range_band']]
    range_used = supported(pool)
    if not range_used:
        pool = selected
    age_used = False
    if query['age_band'] is not None:
        aged = [r for r in pool if r['age_band'] == query['age_band']]
        if supported(aged):
            pool, age_used = aged, True
    own = [abs(r['z']) for r in pool if r['station'] == station]
    knots = parent.NORMAL_KNOTS.copy()
    enough = len(own) >= SPEC['minimum_station_dates'] and supported(pool)
    if enough:
        prior = SPEC['shape_pool_prior']
        q = (len(own)*np.quantile(own, parent.SPEC['coverages']) +
             prior*np.quantile([abs(r['z']) for r in pool], parent.SPEC['coverages']))/(len(own)+prior)
        knots[1:] = np.clip(q, parent.SPEC['shape_scale_bounds'][0]*parent.NORMAL_KNOTS[1:],
                           parent.SPEC['shape_scale_bounds'][1]*parent.NORMAL_KNOTS[1:])
        for i in range(1, len(knots)):
            knots[i] = max(knots[i], knots[i-1]+1e-6)
    else:
        # A sparse condition must not throw away a well-supported global fit.
        global_fit = parent.fit_shape(selected, target, [station])
        knots = np.asarray(global_fit['stations'][station]['knots'])
        range_used, age_used = False, False
    return {'knots': knots.tolist(), 'station_dates': len(own), 'pool_events': len(pool),
            'range_condition_used': range_used, 'age_condition_used': age_used,
            'age_status': 'conditioned_on_verified_age' if age_used else 'unknown_age' if query['age_band'] is None else 'insufficient_verified_age_support',
            'fallback': None if enough else 'global_shape', 'errors_hash': digest(selected), 'selected_context_hash': digest(pool)}


def replay(rows):
    reference = parent.replay(rows)
    lookup = {(p['station'], p['target_date']): p for p in reference['predictions']}
    by_day = defaultdict(list)
    for row in rows:
        by_day[row['target_date']].append(row)
    reference_errors, shape_errors, predictions, models = [], [], [], {}
    stations = sorted({r['station'] for r in rows})
    for target, today in sorted(by_day.items()):
        if not any((r['station'], target) in lookup for r in today):
            continue
        reliability = fit_reliability(reference_errors, target, stations)
        global_shape = parent.fit_shape(shape_errors, target, stations)
        contexts = {r['station']: context(r) for r in today}
        shapes = {station: fit_context_shape(shape_errors, target, station, query) for station, query in contexts.items()}
        model = {'target_date': target, 'label_target_cutoff': parent.cutoff(target),
                 'reliability': reliability, 'global_shape': {k:v for k,v in global_shape.items() if k != 'errors'},
                 'context_shapes': shapes}
        model_id = digest(model); models[model_id] = model
        pending_reference, pending_shape = [], []
        for row in today:
            p = lookup[row['station'], target]
            station, raw, observed = row['station'], row['forecast_high_f'], row['observed_high_f']
            weight = reliability['stations'][station]['weight']
            mu = raw + weight*(p['mean_f']-raw)
            prediction = {**p, 'reference_model_id': p['model_id'], 'model_id': model_id,
                          'reference_mean_f': p['mean_f'], 'reference_knots': p['knots'], 'raw_mean_f': raw,
                          'correction_weight': weight, 'mean_f': mu, 'context': contexts[station],
                          'global_knots': global_shape['stations'][station]['knots'], 'knots': shapes[station]['knots']}
            predictions.append(prediction)
            common = {'station': station, 'target_date': target, 'label_target_cutoff': parent.cutoff(target)}
            pending_reference.append({**common, 'raw_error_f': observed-raw, 'correction_f': p['mean_f']-raw})
            pending_shape.append({**common, 'z': (observed-mu)/p['sigma_f'],
                                  'range_band': contexts[station]['range_band'], 'age_band': contexts[station]['age_band']})
        reference_errors.extend(pending_reference); shape_errors.extend(pending_shape)
    return {'reference_evidence_hash': digest(reference), 'models': models, 'predictions': predictions,
            'reference_errors': reference_errors, 'shape_errors': shape_errors}


def temperature_evaluation(rows, evidence):
    lookup = {(p['station'], p['target_date']): p for p in evidence['predictions']}
    test = [r for r in rows if SPEC['test_start'] <= r['target_date'] <= SPEC['end']]
    scores = {name: [] for name in VARIANTS}
    for row in test:
        p = lookup[row['station'], row['target_date']]
        params = {'raw_mean_same_width': (p['raw_mean_f'], p['reference_knots']),
                  'reference_gaussian': (p['reference_mean_f'], parent.NORMAL_KNOTS),
                  'pr23': (p['reference_mean_f'], p['reference_knots']),
                  'reliability_same_shape': (p['mean_f'], p['reference_knots']),
                  'reliability_global_shape': (p['mean_f'], p['global_knots']),
                  'reliability_context_shape': (p['mean_f'], p['knots'])}
        for name, (mu, knots) in params.items():
            scores[name].append(parent.temperature_scores(row['observed_high_f'], mu, p['sigma_f'], knots))
    return {'station_dates': len(test), 'dates': sorted({r['target_date'] for r in test}),
            'overall': {name: static.summarize(values) for name, values in scores.items()},
            'by_station': {s: {name: static.summarize([v for r,v in zip(test, values) if r['station']==s])
                                for name,values in scores.items()} for s in SPEC['stations']},
            'paired_vs_pr23': static.paired_uncertainty(test, scores['reliability_context_shape'], scores['pr23']),
            'paired_context_vs_global': static.paired_uncertainty(test, scores['reliability_context_shape'], scores['reliability_global_shape']),
            'correction_weights': dict(Counter(str(lookup[r['station'],r['target_date']]['correction_weight']) for r in test)),
            'age_conditioned_predictions': sum(evidence['models'][lookup[r['station'],r['target_date']]['model_id']]['context_shapes'][r['station']]['age_condition_used'] for r in test)}


def market_evaluation(raw, evidence, as_of):
    panel, quality = parent.matched_panel(raw, evidence, as_of)
    lookup = {(p['station'],p['target_date']):p for p in evidence['predictions']}
    for v in panel:
        p = lookup[v['station'],v['target_date']]
        original = {**p, 'mean_f':p['reference_mean_f']}
        v['reference'] = parent.probabilities(v['rows'], original, parent.NORMAL_KNOTS)
        v['pr23'] = parent.probabilities(v['rows'], original, p['reference_knots'])
    report = parent.market_evaluation(panel)
    test = [v for v in panel if parent.SPEC['market_test_start'] <= v['target_date'] <= parent.SPEC['market_test_end']]
    if test:
        report['scores']['pr23'] = parent.aggregate(parent.event_scores(test,lambda v:v['pr23']))
        report['cost_sensitivity']['pr23'] = parent.quote_diagnostics(test,lambda v:v['pr23'])
    return report, panel, quality


def verify_artifact(artifact, identity):
    if artifact.get('identity') != identity or artifact.get('artifact_id') != digest({k:v for k,v in artifact.items() if k!='artifact_id'}):
        raise ValueError('RELIABILITY_ARTIFACT_MISMATCH')
    if any(artifact.get(k) is not False for k in ('historical_as_of_verified','live_ready','paper_execution_enabled')):
        raise ValueError('RELIABILITY_ELIGIBILITY_MISMATCH')
    for model_id, model in artifact['models'].items():
        target=model['target_date']
        if model_id != digest(model) or model['label_target_cutoff'] != parent.cutoff(target):
            raise ValueError('RELIABILITY_MODEL_LINEAGE_MISMATCH')
        refs=earlier(artifact['reference_errors'],target,SPEC['reliability_window_days'])
        errors=earlier(artifact['shape_errors'],target,SPEC['shape_window_days'])
        if model['reliability']['errors_hash']!=digest(refs) or model['global_shape']['errors_hash']!=digest(errors):
            raise ValueError('RELIABILITY_ERROR_LINEAGE_MISMATCH')
    for p in artifact['predictions']:
        model=artifact['models'][p['model_id']]
        if model['target_date']!=p['target_date'] or p['correction_weight']!=model['reliability']['stations'][p['station']]['weight']:
            raise ValueError('RELIABILITY_PREDICTION_LINEAGE_MISMATCH')
        if not math.isclose(p['mean_f'],p['raw_mean_f']+p['correction_weight']*(p['reference_mean_f']-p['raw_mean_f']),abs_tol=1e-10):
            raise ValueError('RELIABILITY_PREDICTION_MEAN_MISMATCH')
        if p['knots']!=model['context_shapes'][p['station']]['knots'] or p['global_knots']!=model['global_shape']['stations'][p['station']]['knots']:
            raise ValueError('RELIABILITY_PREDICTION_SHAPE_MISMATCH')
        for k in ('knots','global_knots','reference_knots'): parent.validate_knots(p[k])


def run(history, market_path, output):
    history, output = Path(history), Path(output)
    data_bytes=(history/'dataset.json').read_bytes(); status_bytes=(history/'latest.json').read_bytes()
    market_bytes=Path(market_path).read_bytes(); status=json.loads(status_bytes)
    if status.get('status') not in ('complete','complete_with_gaps') or status.get('model')!='gfs_global':
        raise ValueError('COMPLETED_GFS_HISTORY_REQUIRED')
    write_once(output/'spec.json',SPEC)
    identity={'spec_hash':digest(SPEC),'implementation_hash':implementation_hash(),
              'input_sha256':hashlib.sha256(data_bytes).hexdigest(),'status_sha256':hashlib.sha256(status_bytes).hexdigest(),
              'market_input_sha256':hashlib.sha256(market_bytes).hexdigest(),'numpy':np.__version__,'scipy':scipy.__version__}
    run_id=digest(identity); report_path=output/(run_id+'.json')
    if report_path.exists():
        report=json.loads(report_path.read_text(encoding='utf-8'))
        if report.get('identity')!=identity or report.get('report_hash')!=digest({k:v for k,v in report.items() if k!='report_hash'}):
            raise ValueError('EXISTING_RELIABILITY_REPORT_MISMATCH')
        artifact=json.loads((output/(report['artifact_id']+'_replay.json')).read_text(encoding='utf-8'))
        verify_artifact(artifact,identity)
        if artifact['artifact_id']!=report['artifact_id']: raise ValueError('RELIABILITY_REPORT_LINK_MISMATCH')
        return report
    now=datetime.now(timezone.utc).isoformat()
    rows=static.validate(json.loads(data_bytes),status['dataset_hash'],now)
    rows=[r for r in rows if SPEC['start']<=r['target_date']<=SPEC['end']]
    static.split(rows)
    artifact={**replay(rows),'identity':identity,'created_at':now,'historical_as_of_verified':False,
              'live_ready':False,'paper_execution_enabled':False}
    market=json.loads(market_bytes)
    market_report,panel,quality=market_evaluation(market['v3'] if isinstance(market,dict) else market,artifact,now)
    artifact['market_panel']=panel; artifact['artifact_id']=digest(artifact)
    verify_artifact(artifact,identity)
    write_once(output/(artifact['artifact_id']+'_replay.json'),artifact)
    report={'run_id':run_id,'identity':identity,'artifact_id':artifact['artifact_id'],'created_at':now,'spec':SPEC,
            'status':'offline_challenger_tested_no_promotion',
            'coverage':{'source_station_dates':len(rows),'source_dates':len({r['target_date'] for r in rows}),
                        'stations':sorted({r['station'] for r in rows}),'market_quality':quality},
            'temperature':temperature_evaluation(rows,artifact),'market':market_report,
            'age_effect_status':'not_estimable_from_archive_without_actual_decision_times',
            'live_ready':False,'paper_execution_enabled':False,'profitability_proven':False,
            'frozen_daytime_changed':False,'hosted_backfill_changed':False,
            'limitations':['Previously inspected months; no untouched-test or forward-performance claim.',
                          'Historical run publication and CLI label availability unverified; three-day maturity lag assumed.',
                          'No age labels invented; verified forward observations are needed before fitting age-specific effects.',
                          'Corrections and widths are selected using earlier events only; repeated research creates selection risk.',
                          'The price guard can match market and abstain; this is not an edge. Costs are quote-only sensitivities.',
                          'No historical importer, in-progress archive, frozen paper book, or existing shadow recipe changed.']}
    report['report_hash']=digest(report); write_once(report_path,report)
    return report
