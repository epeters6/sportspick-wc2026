import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from backend.weather_stage_report import StageReport


class StageReportTests(unittest.TestCase):
    def make(self, directory):
        report = {'mode': 'shadow', 'live_ready': False, 'paper_execution_enabled': False,
                  'stages': {}, 'errors': []}
        db = Mock()
        progress = StageReport(report, directory, db=db, source='test',
                               clock=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc),
                               timer=iter([10, 15, 20, 23]).__next__)
        return report, progress, db

    def test_progress_is_durable_before_work_and_after_partial_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            report, progress, db = self.make(directory)
            with progress.stage('labels'):
                saved = json.loads((Path(directory) / 'latest.json').read_text())
                self.assertEqual(saved['status'], 'running')
                self.assertIsNone(saved['completed_at'])
                raise TimeoutError('must not expose arbitrary provider details')
            with progress.stage('collection'):
                report['stages']['collection'] = {'snapshots': 4, 'errors': []}
            progress.save(complete=True)
            saved = json.loads((Path(directory) / 'latest.json').read_text())
            self.assertEqual(saved['status'], 'degraded')
            self.assertEqual(saved['stage_progress']['labels']['elapsed_seconds'], 5)
            self.assertEqual(saved['stages']['collection']['snapshots'], 4)
            self.assertEqual(saved['errors'], [{'stage': 'labels', 'reason': 'TimeoutError'}])
            self.assertFalse(saved['live_ready'])
            self.assertGreaterEqual(db.table.return_value.upsert.call_count, 5)

    def test_nested_collection_errors_make_the_run_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            report, progress, _ = self.make(directory)
            with progress.stage('collection'):
                report['stages']['collection'] = {'errors': [{'reason': 'RATE_LIMIT'}]}
            progress.save(complete=True)
            self.assertEqual(report['status'], 'degraded')
            self.assertEqual(report['stage_progress']['collection']['status'], 'failed')

    def test_termination_retains_completed_stage_and_interrupted_status(self):
        with tempfile.TemporaryDirectory() as directory:
            report, progress, _ = self.make(directory)
            report['stages']['training'] = {'model_id': 'saved-model'}
            with self.assertRaises(SystemExit):
                with progress.stage('collection'):
                    progress.interrupt(15)
            saved = json.loads((Path(directory) / 'latest.json').read_text())
            self.assertEqual(saved['status'], 'degraded')
            self.assertIsNotNone(saved['completed_at'])
            self.assertEqual(saved['stage_progress']['collection']['status'], 'interrupted')
            self.assertEqual(saved['stages']['training']['model_id'], 'saved-model')
