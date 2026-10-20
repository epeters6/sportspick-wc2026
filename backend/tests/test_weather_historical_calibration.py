import gzip
import json
from datetime import date

import pytest

from backend.ml.weather_learning.dataset import digest
from backend.weather_historical_calibration import (
    ARCHIVE, CLIMATE, STATIONS, CLI_CODES, ReceiptCache, RequestBudgetExceeded, batch_parameters,
    climate_labels, collect_all_stations, collect_label_history, evaluate, make_example, split_batch, standard_day,
)


def test_standard_day_does_not_follow_dst():
    start, end = standard_day("KPHL", date(2026, 7, 1))
    assert start.isoformat() == "2026-07-01T05:00:00+00:00"
    assert (end - start).total_seconds() == 86400
    assert standard_day("KPHX", date(2026, 7, 1))[0].hour == 7


def test_archive_requires_full_day_and_never_claims_public_availability():
    start, _ = standard_day("KPHL", date(2026, 9, 1))
    data = {"hourly_units": {"temperature_2m": "°F"},
            "hourly": {"time": [int(start.timestamp()) + 3600 * i for i in range(24)],
                       "temperature_2m": list(range(50, 74))}}
    receipt = {"data": data, "content_hash": digest(data), "received_at": "2026-10-20T00:00:00Z"}
    label = {"high": 74, "low": 49, "product": "CLI", "link": "/CLI"}
    row = make_example("KPHL", date(2026, 9, 1), "2026-08-31T00:00", receipt, label, receipt)
    assert row["forecast_high_f"] == 73
    assert row["as_of_eligible"] is False
    assert row["production_training_eligible"] is False
    data["hourly"]["temperature_2m"][4] = None
    with pytest.raises(ValueError, match="INCOMPLETE_STANDARD_DAY"):
        make_example("KPHL", date(2026, 9, 1), "2026-08-31T00:00", receipt, label, receipt)


def test_cache_corruption_is_preserved_and_does_not_refetch(tmp_path):
    params = {"station": "KPHL"}
    path = tmp_path / (digest({"url": "example", "params": params}) + ".json.gz")
    with gzip.open(path, "wt") as stream:
        json.dump({"url": "example", "params": params, "data": [], "content_hash": "wrong"}, stream)
    before = path.read_bytes()
    cache = ReceiptCache(tmp_path, budget=0, pace=0)
    with pytest.raises(ValueError, match="HISTORICAL_RECEIPT_INVALID"):
        cache.get("example", params)
    assert path.read_bytes() == before
    assert cache.downloaded == 0
    with pytest.raises(RequestBudgetExceeded):
        cache.get("other", params)


def test_climate_station_identity_and_missing_labels():
    with pytest.raises(ValueError, match="CLIMATE_STATION_MISMATCH"):
        climate_labels({"data": {"results": [{"station": "KORD"}]}}, "KPHL")
    assert climate_labels({"data": {"results": [{"station": "KPHL", "valid": "2026-09-01",
                           "product": "CLI", "link": "/CLI", "high": None, "low": 50}]}}, "KPHL") == {}


def test_calibration_fits_earlier_dates_only():
    rows = [{"target_date": f"2026-09-{day:02d}",
             "forecast_high_f": 70, "observed_high_f": 72 if day <= 21 else 80,
             "forecast_low_f": 50, "observed_low_f": 52 if day <= 21 else 60}
            for day in range(1, 29)]
    result = evaluate(rows)
    assert result["scores"]["high"]["training_bias_f"] == 2
    assert result["scores"]["high"]["corrected_test_mae_f"] == 8
    assert result["train_dates"][-1] < result["test_dates"][0]
    assert result["test_station_date_events"] == 7
    assert result["profitability_evaluated"] is False


def test_batch_rejects_reordered_or_mismatched_locations():
    locations = [{"lat": 40, "lon": -75, "elev": 2}, {"lat": 34, "lon": -118, "elev": 30}]
    data = [{"latitude": 40, "longitude": -75, "elevation": 2, "utc_offset_seconds": 0},
            {"latitude": 34, "longitude": -118, "elevation": 30, "utc_offset_seconds": 0, "location_id": 1}]
    receipt = {"data": data, "content_hash": digest(data)}
    assert split_batch(receipt, locations)[1]["content_hash"] == receipt["content_hash"]
    with pytest.raises(ValueError, match="LOCATION_ID_MISMATCH"):
        split_batch({"data": list(reversed(data))}, locations)
    data[0]["latitude"] = 41
    with pytest.raises(ValueError, match="LOCATION_MISMATCH"):
        split_batch(receipt, locations)
    with pytest.raises(ValueError, match="LENGTH_MISMATCH"):
        split_batch({"data": data[:1]}, locations)


def test_shared_calendar_split_does_not_move_when_station_labels_missing():
    rows = [{"target_date": f"2026-09-{day:02d}", "forecast_high_f": 70, "observed_high_f": 72,
             "forecast_low_f": 50, "observed_low_f": 52} for day in range(1, 29) if day not in (1, 2)]
    result = evaluate(rows, test_start_date="2026-09-22")
    assert result["test_dates"][0] == "2026-09-22"
    assert result["train_station_date_events"] == 19
    assert result["test_station_date_events"] == 7


def test_annual_label_history_accounts_for_leap_year_without_inventing_forecasts(tmp_path):
    class Cache:
        downloaded = 0
        hits = 0

        def get(self, url, params):
            assert url == CLIMATE
            self.downloaded += 1
            data = {"results": [{"station": params["station"], "valid": f"{params['year']}-01-01",
                                "product": "CLI", "link": "/CLI", "high": 70, "low": 50}]}
            return {"data": data, "content_hash": digest(data), "received_at": "2026-10-20T00:00:00Z"}

    result = collect_label_history(tmp_path, [2024, 2025], cache=Cache())
    assert result["accepted_station_date_events"] == 42
    assert result["missing_station_dates"] == 21 * (366 + 365 - 2)
    assert result["status"] == "complete_with_gaps"
    assert "2024-02-29" in result["coverage"]["KPHL"]["2024"]["missing_target_dates"]
    assert result["forecast_error_training_pairs"] == 0
    rows = json.loads((tmp_path / "dataset.json").read_text())
    assert all(not row["forecast_available"] and not row["production_training_eligible"] for row in rows)


def test_batched_collection_resumes_from_receipts_without_duplicates(tmp_path):
    from datetime import datetime, timezone, timedelta
    stations = sorted(CLI_CODES.values())
    locations = [{"icaoId": station, "lat": 30 + i * .2, "lon": -80 - i * .2, "elev": i}
                 for i, station in enumerate(stations)]

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        calls = 0

        def get(self, url, params, **kwargs):
            self.calls += 1
            if url == STATIONS:
                return Response(locations)
            if url == CLIMATE:
                return Response({"results": [{"station": params["station"], "valid": f"2026-09-{i:02d}",
                    "product": "CLI", "link": "/CLI", "high": 80, "low": 50} for i in (1, 2)]})
            assert url == ARCHIVE
            assert params == batch_parameters(locations, date.fromisoformat(params["run"][:10]) + timedelta(days=1))
            start = datetime.fromisoformat(params["run"]).replace(tzinfo=timezone.utc)
            return Response([{"latitude": row["lat"], "longitude": row["lon"], "elevation": row["elev"],
                "utc_offset_seconds": 0, "location_id": i,
                "hourly_units": {"temperature_2m": "°F"}, "hourly": {
                    "time": [int(start.timestamp()) + hour * 3600 for hour in range(72)],
                    "temperature_2m": [60] * 72}} for i, row in enumerate(locations)])

    session = Session()
    first = collect_all_stations(tmp_path / "out", date(2026, 9, 1), 2,
                                cache=ReceiptCache(tmp_path / "cache", budget=23, session=session, pace=0))
    assert first["status"] == "partial_or_blocked"
    assert first["accepted_station_date_events"] == 21
    assert first["dates_processed"] == 1
    second = collect_all_stations(tmp_path / "out", date(2026, 9, 1), 2,
                                 cache=ReceiptCache(tmp_path / "cache", budget=1, session=session, pace=0))
    assert second["status"] == "complete"
    assert second["accepted_station_date_events"] == 42
    assert second["requests_downloaded"] == 1
    assert second["cache_hits"] == 23
    rows = json.loads((tmp_path / "out" / "dataset.json").read_text())
    assert len({(row["station"], row["target_date"]) for row in rows}) == 42
    assert all(not row["production_training_eligible"] and not row["as_of_eligible"] for row in rows)
