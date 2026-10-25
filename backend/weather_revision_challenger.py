"""Offline market-offset research using verified revisions; no collector or trading writes."""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp, ndtr

from backend import weather_fresh_run_evaluation as audit
from backend import weather_model_lead_calibration as evidence
from backend import weather_verified_runs as runs
from backend.ml.weather_learning.dataset import digest
from backend.weather_probability_screen import write_once


SPEC = {
    'version': 'weather_revision_challenger_v1',
    'models': ['gfs_global', 'gfs_hrrr', 'ecmwf_ifs025'],
    'first_target': '2026-10-27', 'train_end': '2026-11-09',
    'validation_start': '2026-11-12', 'validation_end': '2026-11-18',
    'test_start': '2026-11-21', 'test_end': '2026-11-27',
    'features': ['gfs_revision', 'hrrr_revision', 'ecmwf_revision', 'consensus_minus_market'],
    'kernel_width_f': 3.0, 'coefficient_bound': 2.0, 'ridge': 0.1,
    'maximum_probability_change': 0.025, 'minimum_gain': 0.001,
    'minimum_train_dates': 14, 'minimum_train_station_dates': 100,
    'minimum_validation_dates': 7, 'minimum_validation_station_dates': 50,
    'minimum_stations': 10, 'minimum_test_dates': 7, 'minimum_test_station_dates': 50,
    'minimum_fraction_validation_dates_improved': 0.7,
    'minimum_quote_edge': 0.05, 'slippage_stress': [0.0, 0.01, 0.03],
    'collector_extension_authorized': False, 'mode': 'offline_research',
    'paper_execution_enabled': False, 'live_ready': False,
}


def implementation_hash():
    return digest({'source': Path(__file__).read_text(encoding='utf-8').replace('\r\n', '\n'),
                   'verified_reader': evidence.implementation_hash()})


def verify_protocol(protocol):
    evidence.verify(protocol, 'protocol_id')
    if protocol['spec'] != SPEC or protocol['implementation_hash'] != implementation_hash():
        raise ValueError('REVISION_PROTOCOL_CHANGED')
    if runs.timestamp(protocol['created_at']) >= runs.timestamp(SPEC['first_target']+'T00:00Z')-timedelta(days=1):
        raise ValueError('REVISION_PROTOCOL_TOO_LATE')


def freeze(output, now=None):
    path = Path(output)/'protocol.json'
    if path.exists():
        saved = evidence.read(path); verify_protocol(saved); return saved
    now = now or datetime.now(timezone.utc)
    protocol = evidence.signed({'spec': SPEC, 'created_at': now.isoformat(),
        'implementation_hash': implementation_hash()}, 'protocol_id')
    verify_protocol(protocol); write_once(path, protocol)
    return protocol


def market_vector(event):
    market = np.asarray(event['vectors']['market'], dtype=float)
    if (market.ndim != 1 or len(market) < 2 or not np.isfinite(market).all()
            or np.any(market < 0) or not math.isclose(float(market.sum()), 1., abs_tol=1e-10)):
        raise ValueError('REVISION_INVALID_MARKET_VECTOR')
    return market


def features(event):
    """Kernel masses are fixed features, NOT calibrated weather probabilities.

    Current and previous runs use identical basis functions. Agreement discounts
    every correction; identical highly correlated weather models are not counted
    as independent evidence or assigned independent sample weight.
    """
    market = market_vector(event)
    pairs = event['verified_runs']
    if set(pairs) != set(SPEC['models']):
        return None, 'MISSING_VERIFIED_MODEL_PAIR'
    if len(event['contracts']) != len(market):
        raise ValueError('REVISION_CONTRACT_DIMENSION')
    width = SPEC['kernel_width_f']
    current, revisions, highs = [], [], []
    for name in SPEC['models']:
        pair = pairs[name]
        values = []
        for which in ('current', 'previous'):
            run = pair[which]
            high = run['high_f']
            if not runs.finite(high): raise ValueError('REVISION_NONFINITE_TEMPERATURE')
            values.append(np.array([ndtr(((c['high_f'] if c['high_f'] is not None else math.inf)-high)/width)
                - ndtr(((c['low_f'] if c['low_f'] is not None else -math.inf)-high)/width)
                for c in event['contracts']]))
        if not math.isclose(pair['high_revision_f'], pair['current']['high_f']-pair['previous']['high_f'], abs_tol=1e-10):
            raise ValueError('REVISION_DIFFERENCE_MISMATCH')
        if any(not np.isfinite(v).all() or np.any(v < 0) or not math.isclose(float(v.sum()), 1., abs_tol=1e-10) for v in values):
            raise ValueError('REVISION_INVALID_KERNEL_PARTITION')
        current.append(values[0]); revisions.append(values[0]-values[1]); highs.append(pair['current']['high_f'])
    agreement = 1./(1.+float(np.std(highs))/width)
    x = np.column_stack([*revisions, np.mean(current, axis=0)-market])*agreement
    return x, None


def raw_probability(market, x, coefficients):
    # Exact zero market support stays zero; no pseudo-probability invents tail mass.
    if not np.any(coefficients): return market.copy()
    positive = market > 0
    logits = np.log(market[positive])+x[positive]@coefficients
    result = np.zeros_like(market)
    result[positive] = np.exp(logits-logsumexp(logits))
    return result


def probability(market, x, coefficients):
    if x is None: return market.copy()
    coefficients = np.asarray(coefficients, dtype=float)
    if (coefficients.shape != (len(SPEC['features']),) or not np.isfinite(coefficients).all()
            or np.any(np.abs(coefficients) > SPEC['coefficient_bound']+1e-10)
            or x.shape != (len(market), len(coefficients)) or not np.isfinite(x).all()):
        raise ValueError('REVISION_INVALID_PARAMETERS')
    delta = raw_probability(market, x, coefficients)-market
    largest = float(np.abs(delta).max())
    if largest > SPEC['maximum_probability_change']:
        delta *= SPEC['maximum_probability_change']/largest
    return market+delta


def coverage(rows):
    return evidence.coverage([r['event'] for r in rows])


def weights(rows):
    counts = Counter((r['event']['station'], r['event']['target_date']) for r in rows)
    w = np.array([1./counts[r['event']['station'], r['event']['target_date']] for r in rows])
    return w/w.sum()


def objective(coefficients, rows, w):
    loss = SPEC['ridge']*float(coefficients@coefficients)/2
    gradient = SPEC['ridge']*coefficients.copy()
    for row, weight in zip(rows, w):
        p = raw_probability(row['market'], row['x'], coefficients)
        y = np.asarray(row['outcomes'])
        loss -= weight*math.log(max(float(p@y), 1e-12))
        gradient += weight*(row['x'].T@(p-y))
    return loss, gradient


def fit_coefficients(rows):
    # Same station/date contributes total weight one, regardless of venues/quotes.
    result = minimize(objective, np.zeros(len(SPEC['features'])), args=(rows, weights(rows)),
        jac=True, method='L-BFGS-B', bounds=[(-SPEC['coefficient_bound'], SPEC['coefficient_bound'])]*len(SPEC['features']),
        options={'maxiter': 500, 'ftol': 1e-12, 'gtol': 1e-9})
    if not result.success or not np.isfinite(result.x).all():
        raise ValueError('REVISION_OPTIMIZER_FAILED')
    return result.x


def supported(rows, part):
    c = coverage([r for r in rows if r['x'] is not None])
    return (c['dates'] >= SPEC['minimum_'+part+'_dates']
            and c['station_dates'] >= SPEC['minimum_'+part+'_station_dates']
            and c['stations'] >= SPEC['minimum_stations'])


def summary(rows, coefficients):
    events = [r['event'] for r in rows]
    labels = {r['event']['id']: {'outcomes': r['outcomes']} for r in rows}
    vectors = {r['event']['id']: probability(r['market'], r['x'], coefficients).tolist() for r in rows}
    return audit.summarize(events, labels, {'challenger': vectors})['challenger']


def validation_gate(rows, coefficients):
    result = summary(rows, coefficients)
    dates = sorted({r['event']['target_date'] for r in rows})
    wins = []
    for day in dates:
        daily = summary([r for r in rows if r['event']['target_date']==day], coefficients)
        wins.append(all(daily['minus_market'][m]['mean'] < 0 for m in ('brier', 'log_loss')))
    fraction = float(np.mean(wins)) if wins else 0.
    qualified = supported(rows, 'validation') and fraction >= SPEC['minimum_fraction_validation_dates_improved']
    for metric in ('brier', 'log_loss'):
        comparison = result['minus_market'][metric]
        qualified = (qualified and comparison['mean'] < -SPEC['minimum_gain']
                     and comparison['interval_95'] is not None and comparison['interval_95'][1] < 0)
    return {'passed': bool(qualified), 'fraction_dates_improved': fraction, 'comparison': result}


def partition(events, settlements, protocol, as_of):
    parts = {'train': [], 'validation': [], 'test': []}; gaps = Counter(); support = Counter()
    cutoffs = {'train': runs.timestamp(SPEC['validation_start']+'T00:00Z')-timedelta(days=1),
               'validation': runs.timestamp(SPEC['test_start']+'T00:00Z')-timedelta(days=1), 'test': as_of}
    for event in sorted(events.values(), key=lambda e:e['id']):
        target = event['target_date']; decision = runs.timestamp(event['decision_at'])
        if decision <= runs.timestamp(protocol['created_at']):
            gaps['DECISION_BEFORE_PROTOCOL'] += 1; continue
        if decision > as_of: gaps['DECISION_AFTER_AS_OF'] += 1; continue
        part = next((p for p,lo,hi in [('train',SPEC['first_target'],SPEC['train_end']),
            ('validation',SPEC['validation_start'],SPEC['validation_end']), ('test',SPEC['test_start'],SPEC['test_end'])]
            if lo <= target <= hi), None)
        if part is None: gaps['OUTSIDE_FIXED_WINDOWS_OR_EMBARGO'] += 1; continue
        x, reason = features(event)
        if reason:
            gaps[reason] += 1
            if part == 'train': continue
        else: support[part] += 1
        label = settlements.get(event['id'])
        if label is None: gaps['AWAITING_OFFICIAL_SETTLEMENT'] += 1; continue
        if runs.timestamp(label['settled_at']) >= min(as_of, cutoffs[part]):
            gaps['LABEL_NOT_AVAILABLE_BEFORE_'+part.upper()+'_CUTOFF'] += 1; continue
        market = market_vector(event)
        audit.score(market.tolist(), label['outcomes'])
        # Zero-support observations cannot provide a finite offset-model likelihood.
        if part == 'train' and market[label['outcomes'].index(1)] == 0:
            gaps['ZERO_MARKET_SUPPORT_FOR_OBSERVED_OUTCOME'] += 1; continue
        parts[part].append({'event': event, 'market': market, 'x': x, 'outcomes': label['outcomes'],
                            'settlement_fingerprint': label['fingerprint']})
    return parts, dict(gaps), dict(support)


def fit_and_validate(train, validation):
    candidate = np.zeros(len(SPEC['features']))
    if not supported(train, 'train'):
        return {'fitted': False, 'selected': 'market', 'reason': 'INSUFFICIENT_TRAINING_SUPPORT',
                'candidate_coefficients': candidate.tolist(), 'selected_coefficients': candidate.tolist(), 'gate': None}
    candidate = fit_coefficients(train)
    gate = validation_gate(validation, candidate)
    return {'fitted': True, 'selected': 'revision_challenger' if gate['passed'] else 'market',
            'reason': 'VALIDATION_GAIN' if gate['passed'] else 'NO_QUALIFIED_VALIDATION_GAIN',
            'candidate_coefficients': candidate.tolist(),
            'selected_coefficients': candidate.tolist() if gate['passed'] else [0.]*len(candidate), 'gate': gate}


def evaluate(roots, output, now=None):
    output = Path(output); protocol = evidence.read(output/'protocol.json'); verify_protocol(protocol)
    as_of = now or datetime.now(timezone.utc)
    events, settlements, receipts = audit.load_artifacts(roots)
    feature_inventory = {}
    for identity, event in events.items():
        x, reason = features(event)
        feature_inventory[identity] = {'decision_fingerprint': event['fingerprint'],
            'target_date': event['target_date'], 'reason': reason,
            'features': x.tolist() if x is not None else None,
            'diagnostic_only_not_training_eligibility': True}
    parts, gaps, support = partition(events, settlements, protocol, as_of)
    # An immutable fit identity depends on training/validation only, never test labels.
    fit_identity = digest({'protocol_id': protocol['protocol_id'], 'input': {p: [
        [r['event']['fingerprint'], r['settlement_fingerprint']] for r in parts[p]] for p in ('train','validation')}})
    ready = as_of > runs.timestamp(SPEC['test_start']+'T00:00Z')-timedelta(days=1)
    fit_path = output/(fit_identity+'_fit.json')
    if ready and fit_path.exists():
        fitted = evidence.read(fit_path); evidence.verify(fitted, 'model_id')
        if fitted['identity'] != fit_identity or fitted['protocol_id'] != protocol['protocol_id']:
            raise ValueError('REVISION_FIT_IDENTITY_MISMATCH')
    elif ready:
        fitted = evidence.signed({'identity': fit_identity, 'protocol_id': protocol['protocol_id'],
            'created_at': as_of.isoformat(), 'available_label_cutoff': (runs.timestamp(SPEC['test_start']+'T00:00Z')-timedelta(days=1)).isoformat(),
            **fit_and_validate(parts['train'], parts['validation'])}, 'model_id')
        write_once(fit_path, fitted)
    else:
        fitted = {'fitted': False, 'selected': 'market', 'reason': 'FIXED_TRAINING_VALIDATION_WINDOWS_PENDING',
                  'candidate_coefficients': [0.]*len(SPEC['features']), 'selected_coefficients': [0.]*len(SPEC['features'])}
    selected = np.asarray(fitted['selected_coefficients'])
    result = evidence.signed({'protocol_id': protocol['protocol_id'], 'observed_at': as_of.isoformat(),
        'status': 'offline_replay_evaluated' if fitted['fitted'] else 'awaiting_verified_revision_training_data',
        'artifact_decisions_verified': len(events), 'forecast_receipts_verified': len(receipts),
        'official_settlements_verified': len(settlements), 'eligible_feature_vectors': support,
        'verified_feature_inventory': feature_inventory,
        'coverage': {p:coverage(rows) for p,rows in parts.items()}, 'excluded_or_pending': gaps,
        'decision_fingerprints': sorted(e['fingerprint'] for e in events.values()),
        'settlement_fingerprints': sorted(s['fingerprint'] for s in settlements.values()),
        'fit': fitted, 'retrospective_test': summary(parts['test'], selected),
        'test_support_sufficient': supported(parts['test'], 'test'),
        'replayed_test_vectors': {r['event']['id']: probability(r['market'], r['x'], selected).tolist() for r in parts['test']},
        'live_ready': False, 'paper_execution_enabled': False, 'profitability_proven': False,
        'new_forward_predictions': 0, 'collector_extension_authorized': False,
        'limitations': [
            'Offline replay only. These challenger probabilities were not saved before outcomes.',
            'Current PR24 collection ends November 5 and cannot meet the full training/validation/test windows.',
            'Additional prospective collection needs separate authorization; this code does not extend it.',
            'The 3F kernel is a fixed feature basis, not an estimated or calibrated forecast uncertainty.',
            'Same station/date across venues shares weight. Model agreement is not independent evidence.',
            'Missing models/labels and unsupported cases are coverage gaps, not zero-return trades.',
            'Quote-only costs with 1c/3c sensitivity do not establish fills, profit, or live readiness.',
            'Passing validation would only select an offline challenger, never deploy it.']}, 'evidence_hash')
    write_once(output/(result['evidence_hash']+'_evaluation.json'), result)
    return result
