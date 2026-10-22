import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.run_weather_learning import main

import pytest


@pytest.fixture(autouse=True)
def no_real_catalog(monkeypatch):
    monkeypatch.setattr('backend.weather_collection_runtime.discover_catalog', lambda feeds: ([], [], []))


@pytest.mark.parametrize('stale_file', [False, True])
def test_duplicate_hour_does_not_fail_guard_or_replay_old_rows(tmp_path, monkeypatch, stale_file):
    artifact = {'trained_at':datetime.now(timezone.utc).isoformat(), 'model_id':'prior'}
    (tmp_path/'model').mkdir()
    (tmp_path/'model/training.json').write_text(json.dumps(artifact))
    (tmp_path/'history.json').write_text('[]')
    if stale_file:
        (tmp_path/'snapshots.jsonl').write_text('stale invalid file must not be read')
    monkeypatch.setattr('sys.argv', ['run','--no-persist','--collect','--output',str(tmp_path),'--history',str(tmp_path/'history.json')])
    monkeypatch.setattr('scripts.run_weather_learning.signal.signal', lambda *a:None)
    monkeypatch.setattr('backend.ml.weather_learning.training.load_artifact', lambda _:artifact)
    monkeypatch.setattr('backend.ml.weather_learning.collection.collect', lambda *a,**k: {
        'status':'skipped','reason':'HOURLY_SLOT_ALREADY_ATTEMPTED','snapshots':0,'errors':[]})
    assert main() == 0
    report = json.loads((tmp_path/'latest.json').read_text())
    assert report['stages']['market_guard_forecasts']['status'] == 'skipped'
    assert not list((tmp_path/'market_guard/forecasts').glob('*.json'))


@pytest.mark.parametrize('fault', [None, 'stale_collection', 'corrupt_guard'])
def test_market_guard_runs_before_current_collection_and_failures_are_visible(tmp_path, monkeypatch, fault):
    from datetime import timedelta
    from backend.tests.test_weather_market_guard import history
    artifact = {'trained_at': datetime.now(timezone.utc).isoformat(), 'model_id':'prior-model'}
    (tmp_path/'model').mkdir()
    (tmp_path/'model/training.json').write_text(json.dumps(artifact))
    (tmp_path/'history.json').write_text('[]')
    if fault == 'corrupt_guard':
        (tmp_path/'market_guard').mkdir()
        (tmp_path/'market_guard/latest_model.json').write_text('{"model_id":"../unsafe"}')
    collected = []
    def collect(output, **kwargs):
        now = datetime.now(timezone.utc)
        current = history(days=1, stations=1)
        for row in current:
            m = row['metadata']
            row['resolved_at'] = row['is_correct'] = None
            m['decision_at'] = row['created_at'] = (now-timedelta(days=1) if fault == 'stale_collection' else now).isoformat()
            m['target_date'] = (now+timedelta(days=1)).date().isoformat()
            m['contract']['window_end'] = (now+timedelta(days=2)).isoformat()
        (output/'snapshots.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in current))
        collected.extend(current)
        if fault != 'corrupt_guard':
            guard = json.loads((output/'market_guard/artifact_model.json').read_text())
            assert datetime.fromisoformat(guard['created_at']) < now
        return {'snapshots':len(current), 'errors':[]}
    monkeypatch.setattr('sys.argv', ['run', '--no-persist', '--collect', '--output', str(tmp_path), '--history', str(tmp_path/'history.json')])
    monkeypatch.setattr('scripts.run_weather_learning.signal.signal', lambda *args:None)
    monkeypatch.setattr('backend.ml.weather_learning.training.load_artifact', lambda directory:artifact)
    monkeypatch.setattr('backend.ml.weather_learning.collection.collect', collect)
    assert main() == (1 if fault else 0)
    report = json.loads((tmp_path/'latest.json').read_text())
    assert collected
    forecasts = list((tmp_path/'market_guard/forecasts').glob('*.json'))
    if fault:
        assert report['status'] == 'degraded' and not forecasts
        failed_stage = 'market_guard_training' if fault == 'corrupt_guard' else 'market_guard_forecasts'
        assert report['stage_progress'][failed_stage]['status'] == 'failed'
    else:
        assert report['stages']['market_guard_forecasts']['forecast_vectors'] == 1
        assert len(forecasts) == 1
        saved = json.loads(forecasts[0].read_text())
        assert saved['selected'] == 'market' and saved['weight'] == 0
        assert saved['original_row_ids'] == [r['id'] for r in collected]


class LearningRunnerTests(unittest.TestCase):
    def test_collection_failure_leaves_current_report_with_cached_model_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'model').mkdir()
            artifact = {'trained_at': datetime.now(timezone.utc).isoformat(), 'model_id': 'prior-model'}
            (output / 'model' / 'training.json').write_text(json.dumps(artifact))
            with patch('sys.argv', ['run', '--no-persist', '--collect', '--output', directory]), \
                 patch('scripts.run_weather_learning.signal.signal'), \
                 patch('backend.ml.weather_learning.training.load_artifact', return_value=artifact), \
                 patch('backend.ml.weather_learning.collection.collect', side_effect=TimeoutError):
                code = main()
            report = json.loads((output / 'latest.json').read_text())
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], 'degraded')
            self.assertEqual(report['stages']['training']['model_id'], 'prior-model')
            self.assertEqual(report['errors'], [{'stage': 'collection', 'reason': 'TimeoutError'}])
            self.assertIsNotNone(report['completed_at'])
            self.assertFalse(report['paper_execution_enabled'])

    def test_partial_provider_failure_returns_failure_even_without_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'model').mkdir()
            artifact = {'trained_at': datetime.now(timezone.utc).isoformat()}
            (output / 'model' / 'training.json').write_text(json.dumps(artifact))
            with patch('sys.argv', ['run', '--no-persist', '--collect', '--output', directory]), \
                 patch('scripts.run_weather_learning.signal.signal'), \
                 patch('backend.ml.weather_learning.training.load_artifact', return_value=artifact), \
                 patch('backend.ml.weather_learning.collection.collect', return_value={
                     'snapshots': 12, 'errors': [{'reason': 'PUBLIC_FEED_RATE_LIMIT'}]}):
                self.assertEqual(main(), 1)
            report = json.loads((output / 'latest.json').read_text())
            self.assertEqual(report['stages']['collection']['snapshots'], 12)
            self.assertEqual(report['status'], 'degraded')
