import copy
import json
import math
from datetime import date, timedelta

import numpy as np
import pytest
from scipy.special import ndtri

from backend import weather_daytime_challenger as model
from backend.ml.weather_learning.dataset import digest, implementation_hash
from backend.tests.test_weather_station_distribution import history


def shape_errors(scale=.65):
    return [{'station': station, 'target_date': (date(2026, 7, 1) + timedelta(days=i)).isoformat(),
             'label_target_cutoff': model.cutoff((date(2026, 7, 1) + timedelta(days=i)).isoformat()),
             'z': float(scale * ndtri((i + .5) / 60))}
            for i in range(60) for station in ('KNYC', 'KPHL')]


def test_shape_learns_narrower_center_from_prior_errors_only():
    errors = shape_errors()
    fit = model.fit_shape(errors, '2026-09-03', ['KNYC', 'KPHL'])
    knots = fit['stations']['KPHL']['knots']
    assert .5 * model.NORMAL_KNOTS[1] <= knots[1] < .8 * model.NORMAL_KNOTS[1]
    future = {'station': 'KPHL', 'target_date': '2026-09-02', 'label_target_cutoff': '2026-08-30', 'z': 1e8}
    assert fit == model.fit_shape(errors + [future], '2026-09-03', ['KNYC', 'KPHL'])
    bad = copy.deepcopy(errors)
    bad[-1]['label_target_cutoff'] = bad[-1]['target_date']
    with pytest.raises(ValueError, match='LEAKAGE'):
        model.fit_shape(bad, '2026-09-03', ['KNYC', 'KPHL'])
    with pytest.raises(ValueError, match='DUPLICATE'):
        model.fit_shape(errors * 2, '2026-09-03', ['KNYC', 'KPHL'])


def test_shape_can_widen_and_insufficient_support_preserves_reference():
    fit = model.fit_shape(shape_errors(1.4), '2026-09-03', ['KNYC', 'KPHL', 'KSEA'])
    assert fit['stations']['KPHL']['knots'][1] > model.NORMAL_KNOTS[1]
    assert fit['stations']['KSEA']['knots'] == model.NORMAL_KNOTS.tolist()
    assert fit['stations']['KSEA']['reason'] == 'insufficient_shape_history'


def test_warp_is_monotone_symmetric_and_preserves_calibrated_quantiles():
    knots = (model.NORMAL_KNOTS * np.array([1, .6, .85, 1, 1.1])).tolist()
    values = model.cdf(np.linspace(-40, 40, 2001), 0, 2, knots)
    assert np.all(np.diff(values) >= 0)
    assert values + values[::-1] == pytest.approx(np.ones(len(values)))
    for coverage, q in zip(model.SPEC['coverages'], knots[1:]):
        assert model.cdf(q * 2, 0, 2, knots) - model.cdf(-q * 2, 0, 2, knots) == pytest.approx(coverage)
    assert model.cdf(-math.inf, 0, 2, knots) == 0
    assert model.cdf(math.inf, 0, 2, knots) == 1
    with pytest.raises(ValueError, match='KNOTS'):
        model.cdf(0, 0, 2, [0, 1, 1, 2, 3])


@pytest.mark.parametrize('observed', [0, 1, -5, 25])
def test_identity_warp_scores_match_analytic_gaussian(observed):
    actual = model.temperature_scores(observed, 0, 2, model.NORMAL_KNOTS)
    expected = model.residual.score_gaussian(observed, 0, 2)
    for key in ('crps_f', 'nll', 'coverage_50', 'coverage_90', 'interval_score_90_f'):
        assert actual[key] == pytest.approx(expected[key], abs=1e-8)


def panel():
    # Cheap true outcomes are systematically underpriced in synthetic calibration.
    # Repeated station/date records or venues must never increase support counts.
    return [{'station': f'S{s}', 'target_date': f'2026-09-{21+d:02}', 'decision_at': f'2026-09-{20+d:02}T12:00:00Z',
             'market': [.02, .98], 'candidate': [.8, .2], 'venue': 'kalshi',
             'rows': [{'is_correct': True, 'resolved_at': f'2026-09-{22+d:02}T10:00:00Z',
                       'metadata': {'label_available_at': f'2026-09-{22+d:02}T09:00:00Z'}},
                      {'is_correct': False, 'resolved_at': f'2026-09-{22+d:02}T10:00:00Z',
                       'metadata': {'label_available_at': f'2026-09-{22+d:02}T09:00:00Z'}}]}
            for d in range(9) for s in range(12)]


def test_supported_guard_passes_but_large_changes_remain_bounded():
    p = panel()
    fit = model.fit_guard(p, '2026-09-30T12:00:00Z')
    assert fit['weight'] > 0 and 0 in fit['supported_bands']
    probabilities, reason = model.guarded(p[0], fit)
    assert reason == 'qualified_adjustment'
    assert sum(probabilities) == pytest.approx(1)
    assert max(abs(probabilities - np.array(p[0]['market']))) <= .05000000001
    assert probabilities[0] == pytest.approx(.07)


def test_unsupported_band_and_bad_calibration_fall_back_exactly_to_market():
    p = panel()
    fit = model.fit_guard(p, '2026-09-30T12:00:00Z')
    changed = {**p[0], 'market': [.2, .8]}
    prediction, reason = model.guarded(changed, fit)
    assert reason == 'unsupported_positive_adjustment_band'
    assert prediction.tolist() == changed['market']
    bad = copy.deepcopy(p)
    for v in bad:
        for row in v['rows']:
            row['is_correct'] = not row['is_correct']
    fitted_bad = model.fit_guard(bad, '2026-09-30T12:00:00Z')
    assert fitted_bad['weight'] == 0 and fitted_bad['reason'] == 'no_qualified_gain'
    assert model.guarded(p[0], fitted_bad)[0].tolist() == p[0]['market']


def test_guard_excludes_late_labels_and_test_outcomes():
    p = panel()
    original = model.fit_guard(p, '2026-09-30T12:00:00Z')
    future = copy.deepcopy(p[0])
    future['target_date'] = '2026-10-01'
    future['rows'][0]['is_correct'] = False
    future['rows'][1]['is_correct'] = True
    assert original == model.fit_guard(p + [future], '2026-09-30T12:00:00Z')
    for v in p:
        for row in v['rows']:
            row['metadata']['label_available_at'] = '2026-10-01T00:00:00Z'
    late = model.fit_guard(p, '2026-09-30T12:00:00Z')
    assert late['weight'] == 0 and late['station_dates'] == 0


def test_duplicate_snapshots_and_venues_share_support_and_score_weight():
    p = panel()
    repeated = [*p, *[{**v, 'venue': 'polymarket'} for v in p], *p]
    a = model.fit_guard(p, '2026-09-30T12:00:00Z')
    b = model.fit_guard(repeated, '2026-09-30T12:00:00Z')
    assert a['station_dates'] == b['station_dates'] == 108
    assert a['weight'] == b['weight']
    assert a['market_scores'] == b['market_scores']
    assert a['candidates'] == b['candidates']


def test_insufficient_dates_cannot_be_replaced_with_many_buckets_or_snapshots():
    many = panel()[:12] * 100
    fit = model.fit_guard(many, '2026-09-30T12:00:00Z')
    assert fit['reason'] == 'insufficient_calibration' and fit['weight'] == 0


def test_replay_does_not_use_current_or_future_temperature_labels():
    rows = history('2026-04-03', 130)
    original = model.replay(rows)
    changed = copy.deepcopy(rows)
    for r in changed:
        if r['target_date'] >= '2026-08-01':
            r['observed_high_f'] += 200
    altered = model.replay(changed)
    before = lambda e: [p for p in e['predictions'] if p['target_date'] <= '2026-08-03']
    assert before(original) == before(altered)
    assert [p for p in original['predictions'] if p['target_date'] == '2026-08-08'] != [p for p in altered['predictions'] if p['target_date'] == '2026-08-08']


def test_quote_diagnostics_preserve_venue_separation_and_missing_costs():
    p = panel()[:2]
    for v in p:
        for r in v['rows']:
            r['metadata'].update(executable_cost=.035, ask_depth=2, fee_estimate={'fee_model_version': 'saved'})
    p[1]['venue'] = 'polymarket'
    p[1]['rows'][0]['metadata'].pop('executable_cost')
    report = model.quote_diagnostics(p, lambda v: v['candidate'])
    assert report['by_venue']['kalshi']['0.0']['contracts'] == 1
    assert report['by_venue']['polymarket']['0.0']['missing_cost_evidence_buckets'] == 1
    assert report['actual_fills_verified'] is False
    assert 'hypothetical_unit_pnl' not in report


def test_matching_uses_exact_contracts_and_first_valid_local_slot(monkeypatch):
    from backend.weather_historical_calibration import standard_day
    start, end = standard_day('KPHL', date(2026, 10, 1))
    rows = []
    for i, (lo, hi) in enumerate(((None, 70.5), (70.5, None))):
        contract = {'station': 'KPHL', 'target_date': '2026-10-01', 'metric': 'high',
                    'platform': 'kalshi', 'market_id': f'bucket-{i}', 'authority': 'official',
                    'window_start': start.isoformat(), 'window_end': end.isoformat(), 'low_f': lo, 'high_f': hi}
        rows.append({'outcome': f'bucket-{i}', 'prob': .5, 'metadata': {'contract': contract,
                     'settlement_source': 'official', 'bucket_low_f': lo, 'bucket_high_f': hi,
                     'quote_received_at': '2026-09-30T12:00:00Z'}})
    v = {'station': 'KPHL', 'target_date': '2026-10-01', 'metric': 'high', 'lead_days': 1,
         'venue': 'kalshi', 'decision_at': '2026-09-30T12:01:00Z', 'rows': rows, 'market': [.5, .5]}
    wrong = copy.deepcopy(v)
    wrong['rows'][0]['metadata']['contract']['station'] = 'KNYC'
    repeated = copy.deepcopy(v)
    repeated['decision_at'] = '2026-09-30T15:01:00Z'
    for r in repeated['rows']:
        r['metadata']['quote_received_at'] = '2026-09-30T15:00:00Z'
    monkeypatch.setattr(model, 'forward_panel', lambda *a: ([wrong, v, repeated], {}))
    evidence = {'predictions': [{'station': 'KPHL', 'target_date': '2026-10-01', 'mean_f': 70,
                                'sigma_f': 3, 'knots': model.NORMAL_KNOTS.tolist(), 'model_id': 'test'}]}
    result, quality = model.matched_panel([], evidence, '2026-10-23T00:00:00Z')
    assert len(result) == 1 and result[0]['decision_at'] == v['decision_at']
    assert quality['rejected'] == {'CONTRACT_OR_QUOTE_MISMATCH': 1, 'LATER_REPEATED_SNAPSHOT': 1}


def test_changed_cached_knots_are_rejected_even_if_outer_hash_is_recomputed():
    source = model.replay(history('2026-04-03', 100))
    artifact = {**source, 'identity': {'test': True}, 'historical_as_of_verified': False,
                'live_ready': False, 'paper_execution_enabled': False}
    artifact['artifact_id'] = digest(artifact)
    model.verify_artifact(artifact, {'test': True})
    artifact['predictions'][0]['knots'] = [0, .3, .5, 1, 1.5]
    artifact['artifact_id'] = digest({k: v for k, v in artifact.items() if k != 'artifact_id'})
    with pytest.raises(ValueError, match='PREDICTION_LINEAGE'):
        model.verify_artifact(artifact, {'test': True})


def test_immutable_cached_corruption_fails_without_rewriting(tmp_path):
    source = tmp_path / 'history'; source.mkdir()
    raw, status, market = b'[]', b'{"status":"complete","model":"gfs_global"}', b'[]'
    (source / 'dataset.json').write_bytes(raw); (source / 'latest.json').write_bytes(status)
    market_path = tmp_path / 'market.json'; market_path.write_bytes(market)
    identity = {'spec_hash': digest(model.SPEC), 'implementation_hash': model.implementation_hash(),
                'input_sha256': model.hashlib.sha256(raw).hexdigest(), 'status_sha256': model.hashlib.sha256(status).hexdigest(),
                'market_input_sha256': model.hashlib.sha256(market).hexdigest(), 'numpy': np.__version__, 'scipy': model.scipy.__version__}
    output = tmp_path / 'output'; output.mkdir()
    path = output / (digest(identity) + '.json'); path.write_text('{"broken":true}')
    with pytest.raises(ValueError, match='EXISTING_CHALLENGER_REPORT_MISMATCH'):
        model.run(source, market_path, output)
    assert path.read_text() == '{"broken":true}'


def test_frozen_learning_and_paper_model_identities_stay_unchanged():
    from backend.weather_daytime_paper import verify_model
    from pathlib import Path
    frozen = json.loads((Path(__file__).resolve().parents[2] / 'research/weather_daytime_paper/model.json').read_text())
    verify_model(frozen)
    assert frozen['model_id'] == '7b7d1c759cf762af36dec1d9789ac7666220631e2b6ecb4fd509785da3da6c18'
    assert implementation_hash() == '6715ee6c55f405a5aa75dcb8bfa0dd1dd44e415ac31d231e204e916a4f3c2921'
