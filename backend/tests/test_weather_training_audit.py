import copy
import json
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pytest

from backend.tests.test_weather_learning import history as training_history
from backend.tests.test_weather_market_guard import history
from backend.weather_training_audit import (
    SPEC, calibrate, evaluate, event_key, event_weights, fit_logistic, groups,
    raw_predict, run, score, select,
)


def replicate(rows, count):
    result = []
    for i in range(count):
        for original in rows:
            row = copy.deepcopy(original)
            row['id'] += f'-replica{i}'
            row['metadata']['run_id'] += f'-replica{i}'
            result.append(row)
    return result


def predictive_history():
    rows = training_history(6)
    # Synthetic forecast signal ensures the regularization control has nonzero coefficients.
    for row in rows:
        row['metadata']['raw_model_prob'] = .6 if row['is_correct'] else .2
    return rows


def test_high_low_venues_bucket_counts_share_fixed_event_mass():
    rows = training_history(2)
    extra = copy.deepcopy(rows[:3])
    for r in extra:
        r['metadata'].update(metric='low', platform='polymarket')
    rows += extra
    larger = copy.deepcopy(extra + extra)
    for r in larger:
        r['metadata']['run_id'] += 'many-buckets'
    rows += larger
    weights = event_weights(rows)
    totals = defaultdict(float)
    for row, weight in zip(rows, weights):
        totals[event_key(row)] += weight
    assert len(totals) == 8
    np.testing.assert_allclose(list(totals.values()), 1)
    assert weights.sum() == pytest.approx(8)
    first_event_vectors = [idx for idx in groups(rows) if event_key(rows[idx[0]]) == event_key(rows[0])]
    np.testing.assert_allclose([sum(weights[idx]) for idx in first_event_vectors], 1 / len(first_event_vectors))


def test_repeating_evidence_cannot_weaken_candidate_regularization_or_change_scaler():
    rows = predictive_history()
    more = replicate(rows, 20)
    original, repeated = fit_logistic(rows), fit_logistic(more)
    assert original['training_weight_sum'] == pytest.approx(repeated['training_weight_sum'])
    np.testing.assert_allclose(original['scaler'].mean_, repeated['scaler'].mean_, atol=1e-9)
    np.testing.assert_allclose(original['scaler'].var_, repeated['scaler'].var_, atol=1e-9)
    np.testing.assert_allclose(raw_predict(original, rows), raw_predict(repeated, rows), atol=1e-7)
    legacy = fit_logistic(rows, kind='original_logistic_refit')
    legacy_repeated = fit_logistic(more, kind='original_logistic_refit')
    assert np.max(np.abs(raw_predict(legacy, rows) - raw_predict(legacy_repeated, rows))) > 1e-3


def test_unequal_event_snapshot_counts_do_not_shift_candidate_preprocessing():
    rows = predictive_history()
    same_event = [r for r in rows if event_key(r) == event_key(rows[0])]
    first = fit_logistic(rows)
    oversampled = fit_logistic(rows + replicate(same_event, 40))
    np.testing.assert_allclose(first['scaler'].mean_, oversampled['scaler'].mean_, atol=1e-9)
    np.testing.assert_allclose(raw_predict(first, rows), raw_predict(oversampled, rows), atol=1e-7)


def test_calibration_does_not_refit_features_or_classifier():
    model = fit_logistic(training_history(6))
    before = (model['scaler'].mean_.copy(), model['estimator'].coef_.copy())
    later = training_history(2)
    for row in later:
        row['metadata']['run_id'] += row['metadata']['station']
        row['metadata']['station'] = 'UNSEEN'
        row['metadata']['weather_features'] = {'forecast_mean_f': 999}
    calibrate(model, later)
    np.testing.assert_array_equal(model['scaler'].mean_, before[0])
    np.testing.assert_array_equal(model['estimator'].coef_, before[1])
    assert 'station=UNSEEN' not in model['vectorizer'].get_feature_names_out()


def test_same_event_replication_leaves_probability_scores_unchanged():
    rows = training_history(2)
    for i, row in enumerate(rows):
        row['metadata']['market_vector_prob'] = (.2, .3, .5)[i % 3]
    p = [r['metadata']['market_vector_prob'] for r in rows]
    more = rows + replicate(rows[:6], 30)
    assert score(rows, p) == pytest.approx(score(more, [r['metadata']['market_vector_prob'] for r in more]))


def test_selection_requires_coverage_and_both_scores_without_using_test():
    cal = history(days=8)
    market = {'brier': .4, 'log_loss': .8}
    values = {'candidate': {'brier': .3995, 'log_loss': .7}}
    assert select(values, cal, market)['reason'] == 'no_qualified_calibration_gain'
    values['candidate']['brier'] = .39
    chosen = select(values, cal, market)
    assert chosen['calibration_gate_passed'] and chosen['used_test_outcomes'] is False
    assert chosen['deployment'] == 'none_research_only'
    assert select(values, cal[:20], market)['reason'] == 'insufficient_calibration_coverage'


def test_official_timing_embargo_and_duplicate_identity():
    raw = history(days=16, stations=2)
    report = evaluate(raw + raw[:2], '2026-10-01T00:00:00Z')
    assert report['duplicate_identical_rows_removed'] == 2
    split = report['split']
    for left, right in [('train', 'calibration'), ('calibration', 'test')]:
        assert split[left]['last_label_at'] < split[right]['first_decision_at']
        assert not set(split[left]['dates']) & set(split[right]['dates'])
    assert report['selection']['reason'] == 'insufficient_calibration_coverage'
    changed = copy.deepcopy(raw[0])
    changed['prob'] = .12345
    with pytest.raises(ValueError, match='CONFLICTING_ROW_IDENTITY'):
        evaluate(raw + [changed], '2026-10-01T00:00:00Z')
    future = copy.deepcopy(raw)
    for row in future:
        row['id'] += '-future'
        row['metadata']['run_id'] += '-future'
        row['resolved_at'] = row['metadata']['label_available_at'] = '2027-01-01T00:00:00Z'
    after = evaluate(raw + future, '2026-10-01T00:00:00Z')
    assert report['split'] == after['split']
    assert report['ablations'] == after['ablations']


def test_test_outcomes_cannot_change_parameters_or_selection():
    raw = history(days=16, stations=2)
    before = evaluate(raw, '2026-10-01T00:00:00Z')
    test_dates = before['split']['test']['dates']
    for row in raw:
        if row['metadata']['target_date'] in test_dates:
            row['is_correct'] = not row['is_correct']
            row['metadata']['official_result'] = 'yes' if row['is_correct'] else 'no'
    after = evaluate(raw, '2026-10-01T00:00:00Z')
    assert before['selection'] == after['selection']
    assert before['research_candidate_parameters'] == after['research_candidate_parameters']
    assert before['ablations']['station_date_C_0.1']['test'] != after['ablations']['station_date_C_0.1']['test']


def test_spec_precedes_evaluation_and_report_is_immutable_with_actual_creation(tmp_path, monkeypatch):
    import backend.weather_training_audit as module
    source = tmp_path / 'input.json'
    source.write_text('[]')
    output = tmp_path / 'output'
    def fake_evaluate(raw, cutoff):
        spec_files = list(output.glob('*_spec.json'))
        assert len(spec_files) == 1 and json.loads(spec_files[0].read_text()) == SPEC
        return {'selection': {'deployment': 'none_research_only'}}
    monkeypatch.setattr(module, 'evaluate', fake_evaluate)
    started = datetime.now(timezone.utc)
    report = run(source, output, as_of='2026-10-01T00:00:00Z')
    assert datetime.fromisoformat(report['created_at']) >= started
    assert run(source, output, as_of='2026-10-01T00:00:00Z') == report
    path = output / (report['run_id'] + '.json')
    changed = copy.deepcopy(report)
    changed['selection']['deployment'] = 'modified'
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='EXISTING_REPORT_MISMATCH'):
        run(source, output, as_of='2026-10-01T00:00:00Z')
