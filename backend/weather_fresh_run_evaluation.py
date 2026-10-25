"""Read-only, paired audit of preserved shadow artifacts and optional offline replay."""
from __future__ import annotations

import base64
import gzip
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.special import ndtr

from backend import weather_daytime_paper as paper
from backend import weather_verified_runs as runs
from backend import weather_verified_shadow as shadow
from backend import weather_model_lead_calibration as calibration
from backend.ml.weather_learning.contracts import Contract
from backend.ml.weather_learning.dataset import digest
from backend.weather_probability_screen import write_once


def close_equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(close_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(close_equal(x, y) for x, y in zip(a, b))
    if type(a) in (float, int) and type(b) in (float, int):
        return math.isclose(a, b, abs_tol=1e-12, rel_tol=1e-12)
    return type(a) is type(b) and a == b


def add_unique(store, identity, value):
    if identity in store and store[identity] != value:
        raise ValueError('CONFLICTING_PRESERVED_EVIDENCE')
    store[identity] = value


def load_artifacts(roots):
    events, settlements, receipts, models = {}, {}, {}, {}
    for root in map(Path, roots):
        # Explicit artifact roots only; never glob the historical cache or database.
        directory = root/'reports/weather_verified_shadow'
        model = calibration.read(root/'research/weather_daytime_paper/model.json')
        shadow.verify_parent(model); add_unique(models, model['model_id'], model)
        for path in sorted((directory/'receipt').glob('*.json')):
            packed = calibration.read(path)
            record = json.loads(gzip.decompress(base64.b64decode(packed['gzip_base64'], validate=True)))
            if runs.receipt_identity(record) != packed['receipt_id']:
                raise ValueError('FORECAST_RECEIPT_ID_MISMATCH')
            add_unique(receipts, packed['receipt_id'], record)
        for kind, store, field in (('decision', events, 'id'), ('settlement', settlements, 'decision_id')):
            for path in sorted((directory/kind).glob('*.json')):
                value = calibration.read(path); paper.verify_signed(value, 'fingerprint')
                add_unique(store, value[field], value)
    if set(settlements)-set(events):
        raise ValueError('SETTLEMENT_WITHOUT_SAVED_DECISION')
    for event in events.values():
        pairs = {name: tuple(receipts[pair[which]['receipt_id']] for which in ('current', 'previous'))
                 for name, pair in event['verified_runs'].items()}
        rebuilt = shadow.decision(models[event['model_id']], [Contract(**c) for c in event['contracts']],
            event['quotes'], receipts[event['baseline']['receipt_id']], pairs, event['metadata_receipts'],
            event['auxiliary_rejections'], runs.timestamp(event['decision_at']))
        if not close_equal({k:v for k,v in rebuilt.items() if k!='fingerprint'},
                           {k:v for k,v in event.items() if k!='fingerprint'}):
            raise ValueError('SHADOW_DECISION_REPRODUCTION_MISMATCH')
    for identity, value in settlements.items():
        event = events[identity]
        if (value['decision_fingerprint'] != event['fingerprint'] or value['source'] != shadow.SOURCE
                or value['live_ready'] is not False or value['paper_execution_enabled'] is not False):
            raise ValueError('SHADOW_SETTLEMENT_IDENTITY')
        book = {'pending': None, 'halted_venues': [], 'events': {identity: {'station': event['station'],
            'target': event['target_date'], 'venue': event['venue'], 'status': 'pending', 'cost': 0}}}
        paper.settle(book, event, value['outcomes'], value['official_receipts'], runs.timestamp(value['settled_at']))
        expected = {name: score(vector, value['outcomes']) for name, vector in event['vectors'].items()}
        if not close_equal(expected, value['scores']):
            raise ValueError('SHADOW_SETTLEMENT_SCORE_MISMATCH')
    return events, settlements, receipts


def score(vector, outcomes):
    if (len(vector) != len(outcomes) or any(not runs.finite(p) or not 0 <= p <= 1 for p in vector)
            or not math.isclose(sum(vector), 1, abs_tol=1e-10)
            or any(type(y) is not int or y not in (0, 1) for y in outcomes) or sum(outcomes) != 1):
        raise ValueError('INVALID_PAIRED_VECTOR')
    return {'brier': sum((p-y)**2 for p,y in zip(vector, outcomes)),
            'log_loss': -math.log(max(vector[outcomes.index(1)], 1e-12))}


def station_mean(rows, values):
    grouped = defaultdict(list)
    for row, value in zip(rows, values): grouped[row['station'], row['target_date']].append(value)
    return float(np.mean([np.mean(v) for v in grouped.values()])) if grouped else None


def difference(rows, values):
    grouped = defaultdict(list)
    for row, value in zip(rows, values): grouped[row['station'], row['target_date']].append(value)
    dates = sorted({k[1] for k in grouped})
    result = {'mean': station_mean(rows, values), 'interval_95': None, 'target_dates': len(dates),
              'station_dates': len(grouped)}
    if len(dates) < calibration.EVALUATION['minimum_scoring_dates'] or len(grouped) < calibration.EVALUATION['minimum_station_dates']:
        return result
    origin, blocks = runs.timestamp(dates[0]+'T00:00Z'), defaultdict(list)
    for (_, target), v in grouped.items():
        block = (runs.timestamp(target+'T00:00Z')-origin).days//calibration.EVALUATION['calendar_block_days']
        blocks[block].append(float(np.mean(v)))
    sums, counts = np.array([sum(v) for v in blocks.values()]), np.array([len(v) for v in blocks.values()])
    if len(sums) < 3: return result
    rng = np.random.default_rng(calibration.EVALUATION['seed'])
    indices = rng.integers(len(sums), size=(calibration.EVALUATION['bootstrap_samples'], len(sums)))
    means = sums[indices].sum(axis=1)/counts[indices].sum(axis=1)
    result.update(interval_95=np.quantile(means, [.025, .975]).tolist(), calendar_blocks=len(sums))
    return result


def quoted_costs(rows, vectors, outcomes):
    result = {}
    for venue in sorted({r['venue'] for r in rows}):
        result[venue] = {}
        for stress in calibration.EVALUATION['extra_slippage']:
            selected, costs, pnl, probabilities, won = [], [], [], [], []
            missing, evaluated = 0, 0
            for row, vector, y in zip(rows, vectors, outcomes):
                if row['venue'] != venue: continue
                evaluated += 1
                missing += sum(c is None or d < 1 for c,d in zip(row['costs'], row['depths']))
                candidates = [(p-c-stress, -i, i) for i,(p,c,d) in enumerate(zip(vector, row['costs'], row['depths']))
                    if c is not None and d >= 1 and 0 < c+stress < 1
                    and p-c-stress >= calibration.EVALUATION['minimum_quote_edge']]
                if not candidates: continue
                index = max(candidates)[2]; cost = row['costs'][index]+stress
                selected.append(row); costs.append(cost); pnl.append(y[index]-cost)
                probabilities.append(vector[index]); won.append(y[index])
            reliability = []
            bins = calibration.EVALUATION['reliability_bins']
            for lo, hi in zip(bins, bins[1:]):
                idx = [i for i,p in enumerate(probabilities) if lo <= p < hi or hi == 1 and p == 1]
                sub = [selected[i] for i in idx]
                reliability.append({'low': lo, 'high': hi, 'contracts': len(idx),
                    'mean_probability': station_mean(sub, [probabilities[i] for i in idx]),
                    'win_rate': station_mean(sub, [won[i] for i in idx])})
            result[venue][str(stress)] = {'evaluated_vectors': evaluated, 'selected_contracts': len(selected),
                'missing_cost_or_depth_buckets': missing, 'hypothetical_unit_net': sum(pnl),
                'cost_sum': sum(costs), 'net_per_station_date': difference(selected, pnl),
                'selection_reliability': reliability, 'actual_fills_verified': False}
    return result


def summarize(rows, settlements, vectors_by_arm):
    scored = [r for r in rows if r['id'] in settlements]
    y = [settlements[r['id']]['outcomes'] for r in scored]
    result = {}
    baseline = [score(r['vectors']['market'], label) for r,label in zip(scored,y)]
    for arm, lookup in vectors_by_arm.items():
        paired = [i for i,r in enumerate(scored) if r['id'] in lookup]
        subset = [scored[i] for i in paired]; outcomes = [y[i] for i in paired]
        vectors = [lookup[r['id']] for r in subset]
        scores = [score(v,o) for v,o in zip(vectors,outcomes)]
        result[arm] = {'paired_station_dates': len({(r['station'],r['target_date']) for r in subset}),
            'paired_target_dates': len({r['target_date'] for r in subset}),
            'scores': {m: station_mean(subset, [s[m] for s in scores]) for m in ('brier','log_loss')},
            'same_rows_market': {m: station_mean(subset, [baseline[i][m] for i in paired]) for m in ('brier','log_loss')},
            'minus_market': {m: difference(subset, [s[m]-baseline[i][m] for s,i in zip(scores,paired)]) for m in ('brier','log_loss')},
            'quote_only_cost_sensitivity': quoted_costs(subset, vectors, outcomes)}
    return result


def calibrated_vectors(event, model):
    calibration.verify(model, 'model_id')
    if (model['spec_hash'] != digest(calibration.SPEC) or model['implementation_hash'] != calibration.implementation_hash()
            or model['label_target_cutoff'] != calibration.SPEC['calibration_end']):
        raise ValueError('CALIBRATED_MODEL_SCOPE')
    if model['label_target_cutoff'] >= event['target_date']:
        raise ValueError('CALIBRATED_LABEL_LEAKAGE')
    vectors = {}
    for name, pair in event['verified_runs'].items():
        f = pair['current']
        c = calibration.distribution(model, name, 'high', event['station'], f['high_f'], f['initialization_at'], f['standard_day_start'])
        if c is None: continue
        vectors[name] = [float(ndtr(((bucket['high_f'] if bucket['high_f'] is not None else math.inf)-c['mean_f'])/c['sigma_f'])
                        -ndtr(((bucket['low_f'] if bucket['low_f'] is not None else -math.inf)-c['mean_f'])/c['sigma_f']))
                        for bucket in event['contracts']]
    if set(vectors) == set(calibration.SPEC['models']):
        vectors['calibrated_mixture'] = np.mean(list(vectors.values()), axis=0).tolist()
    return vectors


def evaluate(roots, protocol, output, fit_evidence=None):
    calibration.verify_protocol(protocol)
    events, settlements, receipts = load_artifacts(roots)
    included, excluded = [], Counter()
    for event in events.values():
        if not calibration.EVALUATION['first_target'] <= event['target_date'] <= calibration.EVALUATION['last_target']:
            excluded['OUTSIDE_FIXED_EVALUATION_DATES'] += 1; continue
        if runs.timestamp(event['decision_at']) <= runs.timestamp(protocol['created_at']):
            excluded['DECISION_BEFORE_PROTOCOL'] += 1; continue
        included.append(event)
    included.sort(key=lambda r:r['id'])
    forward = {arm: {r['id']:r['vectors'][arm] for r in included} for arm in shadow.ARMS}
    retrospective, replay_gaps = defaultdict(dict), Counter()
    if fit_evidence is not None:
        calibration.verify(fit_evidence, 'evidence_hash')
        if fit_evidence['protocol_id'] != protocol['protocol_id']:
            raise ValueError('REPLAY_PROTOCOL_MISMATCH')
        for event in included:
            predicted = calibrated_vectors(event, fit_evidence['model'])
            for arm in [*calibration.SPEC['models'], 'calibrated_mixture']:
                if arm in predicted: retrospective[arm][event['id']] = predicted[arm]
                else: replay_gaps[arm] += 1
    revisions = [{"id": e['id'], 'station': e['station'], 'target_date': e['target_date'],
                  'models': {name: {'current_high_f': v['current']['high_f'], 'high_revision_f': v['high_revision_f'],
                      'run_age_hours': v['current']['run_age_hours'],
                      'lead_band': calibration.lead_band(v['current']['initialization_at'], v['current']['standard_day_start'])}
                      for name,v in e['verified_runs'].items()}} for e in included]
    strata = {}
    for large in (False, True):
        sub = [e for e in included if (abs(e['verified_runs']['gfs_global']['high_revision_f']) >= calibration.EVALUATION['revision_split_f']) == large]
        strata['revision_at_least_2f' if large else 'revision_below_2f'] = summarize(sub, settlements, forward)
    result = calibration.signed({'protocol_id': protocol['protocol_id'], 'status': 'scored' if any(e['id'] in settlements for e in included) else 'awaiting_eligible_official_outcomes',
        'artifact_decisions_verified': len(events), 'forecast_receipts_verified': len(receipts),
        'included_decisions': len(included), 'excluded_decisions': dict(excluded),
        'pending_decisions': sum(e['id'] not in settlements for e in included),
        'decision_fingerprints': sorted(e['fingerprint'] for e in events.values()),
        'settlement_fingerprints': sorted(s['fingerprint'] for s in settlements.values()),
        'forward_saved_arms': summarize(included, settlements, forward),
        'revision_strata_diagnostic_only': strata, 'verified_forecast_diagnostics': revisions,
        'offline_calibrated_replay_not_forward': summarize(included, settlements, retrospective),
        'replay_model_id': fit_evidence['model']['model_id'] if fit_evidence else None,
        'replay_gaps': dict(replay_gaps), 'new_calibrated_forward_predictions': 0,
        'live_ready': False, 'paper_execution_enabled': False, 'profitability_proven': False,
        'limitations': ['Only immutable pre-outcome PR24 arms are forward evidence.',
            'New calibrated probabilities reconstructed here are retrospective even if labels were fitted on earlier months.',
            'Ten calendar dates at most; block intervals are exploratory and weather across stations is correlated.',
            'Unsettled and missing evidence are not losses or zero trades. Costs are quote-only, no bankroll or fills.',
            'Guard remains market weight zero; no unsupported disagreement is promoted.',
            'Primary comparison and fixed cohort are unchanged; strata do not select stations or models.']}, 'evidence_hash')
    write_once(Path(output)/(result['evidence_hash']+'_evaluation.json'), result)
    return result
