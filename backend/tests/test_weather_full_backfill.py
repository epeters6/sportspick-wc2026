import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.weather_historical_calibration import ARCHIVE, RequestBudgetExceeded
from scripts.run_weather_full_backfill import FullBackfillCache, run_batch


class Response:
    def raise_for_status(self): pass
    def json(self): return {"ok": True}


class Session:
    calls = 0
    def get(self, *a, **k):
        self.calls += 1
        return Response()


def test_rolling_hourly_allowance_survives_process_restart(tmp_path):
    session = Session()
    first = FullBackfillCache(tmp_path, session=session, hourly_limit=2)
    first.get(ARCHIVE, {"latitude": "40,41", "run": "2026-04-02T00:00"})
    second = FullBackfillCache(tmp_path, session=session, hourly_limit=2)
    with pytest.raises(RequestBudgetExceeded, match="ROLLING"):
        second.get(ARCHIVE, {"latitude": "40", "run": "2026-04-02T06:00"})
    assert session.calls == 1
    assert second.get(ARCHIVE, {"latitude": "40,41", "run": "2026-04-02T00:00"})["data"] == {"ok": True}


def test_previous_usage_is_reserved_and_midnight_does_not_reset_rolling_budget(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "decision_archive_usage.json").write_text(json.dumps({"version": 1, "days": {now.date().isoformat(): 2}}))
    cache = FullBackfillCache(tmp_path, session=Session(), hourly_limit=2)
    with pytest.raises(RequestBudgetExceeded):
        cache.get(ARCHIVE, {"latitude": "40", "run": "2026-04-02T00:00"})
    (tmp_path / "full_backfill_rolling_usage.json").write_text(json.dumps({"version": 1, "reservations": [
        {"at": (now - timedelta(hours=2)).isoformat(), "units": 6}]}))
    cache = FullBackfillCache(tmp_path, session=Session(), rolling_limit=6)
    with pytest.raises(RequestBudgetExceeded, match="ROLLING"):
        cache.get(ARCHIVE, {"latitude": "40", "run": "2026-04-02T00:00"})


def test_preexisting_worker_lock_is_preserved(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    lock = cache / "weather_full_backfill_worker.lock"
    lock.write_text("inspect me")
    with pytest.raises(FileExistsError):
        run_batch(tmp_path / "archive", cache, tmp_path / "market", tmp_path / "output")
    assert lock.read_text() == "inspect me"


def test_corrupt_rolling_ledger_cannot_trigger_network(tmp_path):
    (tmp_path / "full_backfill_rolling_usage.json").write_text('{"version":1,"reservations":[{"at":"2026-01-01T00:00:00Z","units":-1}]}')
    session = Session()
    before = (tmp_path / "full_backfill_rolling_usage.json").read_bytes()
    with pytest.raises(ValueError, match="LEDGER_INVALID"):
        FullBackfillCache(tmp_path, session=session).get(ARCHIVE, {"latitude": "40", "run": "2026-04-02T00:00"})
    assert session.calls == 0
    assert (tmp_path / "full_backfill_rolling_usage.json").read_bytes() == before
