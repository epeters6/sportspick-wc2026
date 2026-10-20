"""Durable runner progress, outside the frozen learning implementation."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import time


class StageReport:
    def __init__(self, report, output, *, db=None, source=None, clock=None, timer=None):
        self.report, self.output, self.db, self.source = report, Path(output), db, source
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timer = timer or time.monotonic
        self.active = None

    def save(self, *, complete=False):
        stamp = self.clock().isoformat()
        self.report['updated_at'] = stamp
        errors = self.report['errors'] or any(s.get('errors') for s in self.report['stages'].values())
        self.report['status'] = ('degraded' if errors else 'healthy') if complete else 'running'
        self.report['completed_at'] = stamp if complete else None
        self.output.mkdir(parents=True, exist_ok=True)
        path = self.output / 'latest.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.report, indent=2, allow_nan=False), encoding='utf8')
        temporary.replace(path)
        if self.db is not None:
            self.db.table('app_settings').upsert(
                {'key': self.source + '_latest', 'value': self.report, 'updated_at': stamp},
                on_conflict='key').execute()

    @contextmanager
    def stage(self, name):
        self.active = name
        started = self.timer()
        self.report.setdefault('stage_progress', {})[name] = {
            'status': 'running', 'started_at': self.clock().isoformat()}
        self.save()
        print(json.dumps({'stage': name, 'status': 'running'}), flush=True)
        try:
            yield
        except Exception as exc:
            self.report['errors'].append({'stage': name, 'reason': type(exc).__name__})
        finally:
            failed = any(e['stage'] == name for e in self.report['errors']) or bool(
                self.report['stages'].get(name, {}).get('errors'))
            self.report['stage_progress'][name].update(
                status='interrupted' if self.report.get('interrupted_at') else ('failed' if failed else 'completed'),
                completed_at=self.clock().isoformat(), elapsed_seconds=round(self.timer() - started, 3))
            self.active = None
            self.save(complete=bool(self.report.get('interrupted_at')))
            print(json.dumps({'stage': name, **self.report['stage_progress'][name]}), flush=True)

    def interrupt(self, signum):
        self.report['interrupted_at'] = self.clock().isoformat()
        self.report['errors'].append({'stage': self.active or 'runner', 'reason': 'RUNNER_INTERRUPTED',
                                      'signal': signum})
        if self.active:
            self.report['stage_progress'][self.active]['status'] = 'interrupted'
        self.save(complete=True)
        raise SystemExit(128 + signum)
