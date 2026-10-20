import gzip
import json
from datetime import date

import pytest

from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_decision_archive import ArchiveBudgetCache, collect, make_plan, run_pair, summarize_run
from backend.weather_historical_calibration import ARCHIVE, CLIMATE, STATIONS, RequestBudgetExceeded, standard_day


def test_run_selection_precedes_assumed_release_cutoff_with_prior_cycle():
    current, previous = run_pair("gfs_global", "2026-09-01T12:00:00Z")
    assert current.isoformat() == "2026-09-01T06:00:00+00:00"
    assert previous.isoformat() == "2026-09-01T00:00:00+00:00"
    current, previous = run_pair("gfs_hrrr", "2026-09-01T12:00:00Z")
    assert current.hour == 6 and previous.hour == 0


def test_plan_uses_daylight_decision_clock_and_standard_target_window():
    plan = make_plan(date(2026, 9, 2), 1, [8], stations=["KPHL"])
    d = plan["decisions"][0]
    assert d["decision_at"] == "2026-09-01T12:00:00+00:00"
    assert standard_day("KPHL", date(2026, 9, 2))[0].hour == 5
    assert plan["production_training_eligible"] is False
    assert plan["historical_availability_verified"] is False
    assert len(plan["requests"]) == 6


def test_overlapping_requests_are_deduplicated_and_plan_changes_identity():
    a = make_plan(date(2026, 9, 2), 1, [8, 11], stations=["KPHL"])
    assert len(a["requests"]) < 12
    b = make_plan(date(2026, 9, 2), 1, [8], stations=["KPHL"])
    assert a["plan_id"] != b["plan_id"]


def receipt(complete=True):
    start, _ = standard_day("KPHL", date(2026, 9, 2))
    data = {"hourly_units": {"temperature_2m": "°F", "dew_point_2m": "°F", "cloud_cover": "%", "wind_speed_10m": "km/h"}, "hourly": {
        "time": [int(start.timestamp()) + 3600*i for i in range(24)],
        "temperature_2m": [50+i for i in range(24)], "dew_point_2m": [45]*24,
        "cloud_cover": [20]*24, "wind_speed_10m": [5]*24}}
    if not complete:
        data["hourly"]["temperature_2m"][23] = None
    return {"data": data, "content_hash": digest(data), "received_at": "2026-10-20T00:00:00Z"}


def test_short_horizon_stays_missing_instead_of_partial_day_extreme():
    good = summarize_run(receipt(), "KPHL", "2026-09-02", "2026-09-01T06:00")
    assert good["low_f"] == 50 and good["high_f"] == 73
    missing = summarize_run(receipt(False), "KPHL", "2026-09-02", "2026-09-01T06:00")
    assert missing["complete_standard_day"] is False
    assert missing["low_f"] is None and missing["high_f"] is None
    assert missing["hours_available"] == 23


def test_daily_budget_survives_restart_and_zero_budget_still_reads_cache(tmp_path):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}
    class Session:
        calls = 0
        def get(self, *a, **k):
            self.calls += 1
            return Response()
    session = Session()
    p = {"latitude": "40,41", "run": "2026-09-01T00:00"}
    c = ArchiveBudgetCache(tmp_path, location_budget=2, daily_location_budget=2, session=session, pace=0)
    c.get(ARCHIVE, p)
    assert c.units_used == 2 and session.calls == 1
    c = ArchiveBudgetCache(tmp_path, location_budget=2, daily_location_budget=2, session=session, pace=0)
    with pytest.raises(RequestBudgetExceeded):
        c.get(ARCHIVE, {**p, "run": "2026-09-01T06:00"})
    assert session.calls == 1
    cached = ArchiveBudgetCache(tmp_path, request_budget=0, location_budget=0, session=session)
    assert cached.get(ARCHIVE, p)["data"] == {"ok": True}
    assert cached.units_used == 0


def test_corrupt_cached_receipt_is_preserved(tmp_path):
    p = {"latitude": "40", "run": "2026-09-01T00:00"}
    path = tmp_path / (digest({"url": ARCHIVE, "params": p}) + ".json.gz")
    with gzip.open(path, "wt") as stream:
        json.dump({"url": ARCHIVE, "params": p, "data": {}, "content_hash": "bad", "received_at": "now"}, stream)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="HISTORICAL_RECEIPT_INVALID"):
        ArchiveBudgetCache(tmp_path, request_budget=0).get(ARCHIVE, p)
    assert path.read_bytes() == before


def test_unavailable_run_is_preserved_and_not_retried(tmp_path):
    import requests
    class Session:
        calls = 0
        def get(self, *a, **k):
            self.calls += 1
            response = requests.Response()
            response.status_code = 400
            response._content = b'{"error":true,"reason":"The requested model run is not available. Model: ncep_hrrr_conus"}'
            return response
    session = Session()
    cache = ArchiveBudgetCache(tmp_path, session=session)
    params = {"latitude": "40", "run": "2026-09-01T06:00"}
    first = cache.get(ARCHIVE, params)
    assert first["unavailable"] is True
    assert cache.get(ARCHIVE, params) == first
    assert session.calls == 1
    missing = summarize_run(first, "KPHL", "2026-09-02", "2026-09-01T06:00")
    assert missing["low_f"] is None and missing["hours_available"] == 0


def test_collector_resumes_without_losing_partial_evidence_and_rejects_bad_units(tmp_path, monkeypatch):
    import backend.weather_historical_calibration as historical
    monkeypatch.setattr(historical.time, "sleep", lambda _: None)

    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Session:
        calls = 0
        def get(self, url, params, **kwargs):
            self.calls += 1
            if url == STATIONS:
                return Response([{"icaoId": "KPHL", "lat": 40, "lon": -75, "elev": 2}])
            if url == CLIMATE:
                return Response({"results": [{"station": "KPHL", "valid": "2026-09-02",
                    "product": "CLI", "link": "/CLI", "high": 74, "low": 49}]})
            assert url == ARCHIVE
            data = receipt()["data"]
            data.update(latitude=40, longitude=-75, elevation=2, utc_offset_seconds=0)
            return Response(data)

    plan = make_plan(date(2026, 9, 2), 1, [8], stations=["KPHL"])
    session = Session()
    output, cache_dir = tmp_path / "output", tmp_path / "receipts"
    partial = collect(plan, output, ArchiveBudgetCache(cache_dir, request_budget=3, session=session))
    assert partial["status"] == "partial_or_blocked" and partial["decision_examples"] == 0
    saved = {p.name: p.read_bytes() for p in output.glob("*_*.json")}
    complete = collect(plan, output, ArchiveBudgetCache(cache_dir, request_budget=10, session=session))
    assert complete["status"] == "complete" and complete["decision_examples"] == 1
    assert complete["all_three_current_complete"] == 1
    for name, data in saved.items():
        assert (output / name).read_bytes() == data
    calls = session.calls
    replay = collect(plan, output, ArchiveBudgetCache(cache_dir, request_budget=0, session=session))
    assert session.calls == calls and replay["requests_downloaded"] == 0
    assert replay["dataset_hash"] == complete["dataset_hash"]
    rows = json.loads((output / complete["dataset_file"]).read_text())
    assert rows[0]["production_training_eligible"] is False
    assert rows[0]["forecasts"]["gfs_global"]["low_revision_f"] == 0

    class InvalidUnitsCache(ArchiveBudgetCache):
        def get(self, url, params):
            result = super().get(url, params)
            if url == ARCHIVE:
                result["data"]["hourly_units"]["temperature_2m"] = "°C"
            return result
    invalid = collect(plan, output, InvalidUnitsCache(cache_dir, request_budget=0, session=session))
    assert invalid["status"] == "partial_or_blocked"
    assert invalid["decision_examples"] == 0
    assert invalid["errors"][0]["reason"] == "ARCHIVE_WEATHER_UNIT_MISMATCH"
    assert (output / complete["dataset_file"]).exists()
