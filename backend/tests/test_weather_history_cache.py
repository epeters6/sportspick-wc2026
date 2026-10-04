import gzip
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from backend.weather_history_cache import refresh_history


class HistoryCacheTests(unittest.TestCase):
    def test_new_rows_and_old_official_labels_are_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.gz'
            now = datetime(2026, 10, 4, tzinfo=timezone.utc)
            old = {'id': 'old', 'created_at': '2026-10-01T00:00:00+00:00', 'resolved_at': None}
            refresh_history(lambda *args: [old], path, ['v3'], clock=lambda: now)
            queries = []
            def delta(source, field, cursor, since):
                queries.append(field)
                return ([{'id': 'new', 'created_at': now.isoformat()}] if field == 'created_at'
                        else [{**old, 'resolved_at': now.isoformat(), 'is_correct': True}])
            rows, stats = refresh_history(delta, path, ['v3'], clock=lambda: now)
            self.assertEqual(queries, ['created_at', 'resolved_at'])
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]['is_correct'])
            self.assertFalse(stats['bootstrap'])

    def test_partial_failure_does_not_advance_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.gz'
            refresh_history(lambda *args: [], path, ['v3'])
            original = path.read_bytes()
            def failed(*args):
                yield {'id': 'partial', 'created_at': '2026-10-04T00:00:00Z'}
                raise RuntimeError('read failed')
            with self.assertRaises(RuntimeError):
                refresh_history(failed, path, ['v3'])
            self.assertEqual(path.read_bytes(), original)

    def test_duplicate_overlap_and_scope_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.gz'
            row = {'id': 'one', 'created_at': '2026-10-04T00:00:00Z'}
            refresh_history(lambda *args: [row], path, ['v3'])
            rows, _ = refresh_history(lambda *args: [row], path, ['v3'])
            self.assertEqual(rows, [row])
            with self.assertRaises(ValueError):
                refresh_history(lambda *args: [], path, ['different'])

if __name__ == '__main__':
    unittest.main()
