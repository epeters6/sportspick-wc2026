import copy
import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from backend.ml.weather_learning.dataset import digest
from backend.weather_historical_calibration import standard_day
from backend.weather_station_distribution import (
    SPEC, distribution, evaluate, fit_metric, observation_scores, run, split, validate,
)


def example(station, day, index=0):
    start, end = standard_day(station, day)
    offset = 6 if station == 'KPHL' else -6
    noise = (index % 5) - 2
    return {'station': station, 'target_date': day.isoformat(), 'model': 'gfs_global',
            'run_initialization_utc': (day - timedelta(days=1)).isoformat() + 'T00:00:00+00:00',
            'standard_day_start': start.isoformat(), 'standard_day_end': end.isoformat(),
            'forecast_high_f': 75, 'forecast_low_f': 50,
            'observed_high_f': 75 + offset + noise, 'observed_low_f': 50 + offset + noise,
            'forecast_receipt_hash': 'forecast', 'label_receipt_hash': 'label',
            'label_product': 'CLI', 'label_link': 'https://example.invalid/official',
            'historical_availability': 'unverified', 'as_of_eligible': False,
            'production_training_eligible': False, 'label_snapshot_is_final_as_retrieved': True,
            'retrieved_at': '2026-10-20T00:00:00+00:00'}


def history(start, days, stations=('KPHL', 'KNYC')):
    return [example(s, date.fromisoformat(start) + timedelta(days=i), i) for i in range(days) for s in stations]


def test_station_bias_learns_opposite_errors_with_shrinkage():
    train = history('2026-04-03', 120)
    cal = history('2026-08-31', 19)
    model = fit_metric(train, cal, 'high')
    assert model['pooled_bias_f'] == pytest.approx(0)
    assert model['stations']['KPHL']['bias_f'] == pytest.approx(4.8)
    assert model['stations']['KNYC']['bias_f'] == pytest.approx(-4.8)
    assert distribution(75, 'KPHL', model, 'station_bias')[0] == pytest.approx(79.8)
    assert distribution(75, 'KNYC', model, 'station_bias')[0] == pytest.approx(70.2)
    with pytest.raises(ValueError, match='UNSUPPORTED'):
        distribution(75, 'UNKNOWN', model, 'station_bias')


def test_uncertainty_uses_calibration_residuals_and_shrinks_toward_pool():
    train, cal = history('2026-04-03', 120), history('2026-08-31', 18)
    for i, row in enumerate(cal):
        bias = 4.8 if row['station'] == 'KPHL' else -4.8
        noise = (8 if (i // 2) % 2 else -8) if row['station'] == 'KPHL' else 0
        row['observed_high_f'] = row['forecast_high_f'] + bias + noise
    model = fit_metric(train, cal, 'high')
    pooled = model['pooled_sigma_f']['station_bias']
    assert 1.5 <= model['stations']['KNYC']['sigma_f'] < pooled < model['stations']['KPHL']['sigma_f'] < 8
    before = {s: values['bias_f'] for s, values in model['stations'].items()}
    for row in cal:
        row['observed_high_f'] += 3
    later = fit_metric(train, cal, 'high')
    assert before == {s: values['bias_f'] for s, values in later['stations'].items()}
    assert model['stations']['KPHL']['sigma_f'] != later['stations']['KPHL']['sigma_f']


def test_remaining_bias_included_in_error_width_and_floor():
    train, cal = history('2026-04-03', 120, ('KPHL',)), history('2026-08-31', 19, ('KPHL',))
    for row in train:
        row['observed_high_f'] = row['forecast_high_f']
    for row in cal:
        row['observed_high_f'] = row['forecast_high_f'] + 4
    model = fit_metric(train, cal, 'high')
    assert model['stations']['KPHL']['sigma_f'] == pytest.approx(4)
    for row in cal:
        row['observed_high_f'] = row['forecast_high_f']
    assert fit_metric(train, cal, 'high')['stations']['KPHL']['sigma_f'] == 1.5


def test_gaussian_crps_known_value_and_coverage_width_tradeoff():
    point = observation_scores(0, 0, 1)
    assert point['crps_f'] == pytest.approx((math.sqrt(2) - 1) / math.sqrt(math.pi))
    assert point['gaussian_nll'] == pytest.approx(.5 * math.log(2 * math.pi))
    narrow, wide = observation_scores(3, 0, 1), observation_scores(3, 0, 4)
    assert narrow['coverage_90'] == 0 and wide['coverage_90'] == 1
    assert wide['width_90_f'] == 4 * narrow['width_90_f']
    assert observation_scores(0, 0, 10)['crps_f'] > point['crps_f']


def test_duplicate_future_and_wrong_source_cannot_enter_fitting():
    train, cal = history('2026-04-03', 120), history('2026-08-31', 19)
    with pytest.raises(ValueError, match='DUPLICATE'):
        fit_metric(train + train[:1], cal, 'high')
    with pytest.raises(ValueError, match='FITTING_DATE_LEAKAGE'):
        fit_metric(train, history('2026-09-20', 1), 'high')
    raw = history('2026-04-03', 2)
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        validate(raw, 'bad-hash', '2026-10-21T00:00:00Z')
    for field, value, reason in [('model', 'ecmwf', 'MODEL_MISMATCH'),
                                 ('observed_high_f', 0, 'DAILY_EXTREMA'),
                                 ('standard_day_start', '2026-04-03T04:00:00Z', 'WINDOW_MISMATCH'),
                                 ('retrieved_at', '2027-01-01T00:00:00Z', 'RECEIVED_AFTER'),
                                 ('as_of_eligible', True, 'ELIGIBILITY_CHANGED')]:
        bad = copy.deepcopy(raw)
        bad[0][field] = value
        with pytest.raises(ValueError, match=reason):
            validate(bad, digest(bad), '2026-10-21T00:00:00Z')


def test_held_back_labels_change_scores_not_fitted_parameters():
    train, cal, test = history('2026-04-03', 120), history('2026-08-31', 19), history('2026-09-20', 29)
    models = {m: fit_metric(train, cal, m) for m in ('high', 'low')}
    preserved = copy.deepcopy(models)
    before = evaluate(test, models)
    for row in test:
        row['observed_high_f'] += 10
    after = evaluate(test, models)
    assert models == preserved
    assert before['high']['overall'] != after['high']['overall']
    assert before['low'] == after['low']
    assert after['high']['paired_vs_pooled']['calendar_blocks'] == 5


def test_complete_run_preserves_spec_model_and_report(tmp_path, monkeypatch):
    import backend.weather_station_distribution as module
    raw = history('2026-04-03', 199, tuple(SPEC['stations']))
    source, output = tmp_path / 'history', tmp_path / 'result'
    source.mkdir()
    (source / 'dataset.json').write_text(json.dumps(raw))
    (source / 'latest.json').write_text(json.dumps({'status': 'complete', 'model': 'gfs_global', 'dataset_hash': digest(raw)}))
    original_evaluate = module.evaluate
    def check_order(test, models):
        assert json.loads((output / 'spec.json').read_text()) == SPEC
        assert len(list(output.glob('*_model.json'))) == 1
        return original_evaluate(test, models)
    monkeypatch.setattr(module, 'evaluate', check_order)
    started = datetime.now(timezone.utc)
    report = run(source, output)
    assert datetime.fromisoformat(report['created_at']) >= started
    assert report['coverage']['train']['station_dates'] == 149 * 21
    assert len(report['coverage']['calibration']['dates']) == 19
    assert len(report['coverage']['test']['dates']) == 29
    assert report['unscored_buffer_or_outside_dates'] == ['2026-08-30', '2026-09-19']
    assert report['test_used_for_fitting_or_selection'] is False
    assert run(source, output) == report
    path = output / f"{report['model_id']}_model.json"
    model = json.loads(path.read_text())
    model['models']['high']['pooled_bias_f'] += 1
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError, match='MODEL_IDENTITY_MISMATCH'):
        run(source, output)


def test_all_stations_required_in_each_partition():
    raw = history('2026-04-03', 199, tuple(SPEC['stations']))
    raw = [r for r in raw if not (r['station'] == 'KPHL' and r['target_date'] >= SPEC['test_start'])]
    with pytest.raises(ValueError, match='INSUFFICIENT_TEST_COVERAGE:KPHL:0'):
        split(raw)
