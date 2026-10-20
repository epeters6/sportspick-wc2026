import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.run_weather_learning import main


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
