import copy
import json
from datetime import date, timedelta

import numpy as np
import pytest

from backend import weather_condition_calibration as model
from backend.ml.weather_learning.dataset import digest, implementation_hash
from backend.tests.test_weather_station_distribution import history


def conditions():
    rows = history('2026-04-03', 110)
    for i, row in enumerate(rows):
        width = (5, 15, 25)[(i // 2) % 3]
        row['forecast_high_f'] = row['forecast_low_f'] + width
        row['observed_high_f'] = row['forecast_high_f'] + (15-width)*.35
    return rows


def test_mean_learns_opposite_range_bias_without_target_or_future_labels():
    rows = conditions()
    fitted = model.fit_mean(rows, '2026-07-01', 'high', ['KNYC', 'KPHL'])
    narrow = {**rows[0], 'forecast_high_f': 55}
    wide = {**rows[0], 'forecast_high_f': 75}
    assert model.mean(narrow, fitted) > 57
    assert model.mean(wide, fitted) < 73
    altered = copy.deepcopy(rows)
    for row in altered:
        if row['target_date'] > '2026-06-28':
            row['observed_high_f'] += 1000
            row['observed_low_f'] -= 1000
    assert fitted == model.fit_mean(altered, '2026-07-01', 'high', ['KNYC', 'KPHL'])


def errors():
    return [{'station': station, 'target_date': (date(2026, 7, 1)+timedelta(days=i)).isoformat(),
             'label_target_cutoff': model.cutoff((date(2026, 7, 1)+timedelta(days=i)).isoformat()),
             'range_band': i % 3, 'error_f': (-9 if i % 3 == 2 else 2), 'model_id': 'test'}
            for i in range(60) for station in ('KNYC', 'KPHL')]


def test_uncertainty_keeps_cold_tail_and_only_mature_prequential_errors():
    values = errors()
    fit = model.fit_errors(values, '2026-08-30', 'low', ['KNYC', 'KPHL'])
    assert max(r['target_date'] for r in fit['errors']) == '2026-08-27'
    cold = model.distribution(fit, 'KPHL', 2)
    warm = model.distribution(fit, 'KPHL', 0)
    assert cold['centers_f'] == [-9]
    assert warm['centers_f'] == [2]
    assert sum(cold['weights']) == pytest.approx(1)
    values[0]['label_target_cutoff'] = values[0]['target_date']
    with pytest.raises(ValueError, match='LEAKAGE'):
        model.fit_errors(values, '2026-08-30', 'low', ['KNYC', 'KPHL'])


def test_low_support_uses_visible_pool_backoff_and_monotone_intervals():
    fit = {'errors': errors()[:40]}
    result = model.distribution(fit, 'KPHL', 2)
    assert result['band_backoff']
    kernel = model.residual.mixture_distribution(result['centers_f'], result['weights'], result['kernel_sigma_f'])
    bounds = [kernel['intervals'][c] for c in ('50','80','90','95')]
    assert all(lo < hi for lo, hi in bounds)
    assert all(a[0] >= b[0] and a[1] <= b[1] for a,b in zip(bounds,bounds[1:]))
    with pytest.raises(ValueError, match='DUPLICATE'):
        model.fit_mean(conditions()*2, '2026-07-01', 'high', ['KNYC','KPHL'])


def test_replay_lineage_and_tampering_fail_closed(monkeypatch):
    rows = history('2026-04-03', 199)
    # Narrow the scoring range only in this synthetic test; earlier predictions
    # still fit and calibrate on their own preceding dates.
    monkeypatch.setitem(model.SPEC, 'test_start', '2026-10-18')
    evidence = {**model.replay(rows), 'identity': {'test': True},
                'historical_as_of_verified': False, 'production_training_eligible': False,
                'live_ready': False, 'paper_execution_enabled': False}
    evidence['artifact_id'] = digest(evidence)
    model.verify(evidence, {'test':True})
    evidence['predictions'][0]['distribution']['weights'][0] += .1
    evidence['artifact_id'] = digest({k:v for k,v in evidence.items() if k!='artifact_id'})
    with pytest.raises(ValueError, match='DISTRIBUTION_MISMATCH'):
        model.verify(evidence, {'test':True})


def test_cached_report_corruption_does_not_refit_or_overwrite(tmp_path, monkeypatch):
    source = tmp_path/'input'
    source.mkdir()
    raw = b'[]'
    status_raw = b'{"status":"complete","model":"gfs_global"}'
    (source/'dataset.json').write_bytes(raw)
    (source/'latest.json').write_bytes(status_raw)
    identity = {'spec_hash':digest(model.SPEC), 'implementation_hash':model.implementation_hash(),
                'input_sha256':model.hashlib.sha256(raw).hexdigest(), 'status_sha256':model.hashlib.sha256(status_raw).hexdigest(),
                'numpy_version':np.__version__, 'scipy_version':model.scipy.__version__}
    output = tmp_path/'output'
    output.mkdir()
    path = output/(digest(identity)+'.json')
    path.write_text('{"tampered":true}')
    with pytest.raises(ValueError, match='EXISTING_CONDITIONAL_REPORT_MISMATCH'):
        model.run(source, output)
    assert path.read_text() == '{"tampered":true}'


def test_frozen_learning_implementation_stays_identical():
    assert implementation_hash() == '6715ee6c55f405a5aa75dcb8bfa0dd1dd44e415ac31d231e204e916a4f3c2921'
