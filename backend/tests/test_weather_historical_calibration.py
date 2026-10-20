import gzip
import json
from datetime import date

import pytest

from backend.ml.weather_learning.dataset import digest
from backend.weather_historical_calibration import (
    ReceiptCache, RequestBudgetExceeded, climate_labels, evaluate, make_example, standard_day,
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
