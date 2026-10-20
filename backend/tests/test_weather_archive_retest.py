import copy
import json
from datetime import date, timedelta

import numpy as np
import pytest

from backend.ml.weather_learning.dataset import digest
from backend.weather_archive_retest import (
    SPEC, archived_probabilities, blend, features, fit_metric, predicted_temperature,
    market_replay, row_weights, run, validated_rows, weather_scores,
)
from backend.weather_decision_archive import MODELS, make_plan
from backend.weather_historical_calibration import standard_day
from backend.tests.test_weather_market_correction import panel


def example(decision):
    row = copy.deepcopy(decision)
    start, end = standard_day(row["station"], date.fromisoformat(row["target_date"]))
    row.update(observed_high_f=74, observed_low_f=52, as_of_eligible=False, production_training_eligible=False,
               label_receipt_hash="official", label_product="CLI", label_link="https://example.invalid/cli", forecasts={})
    for model, runs in row["runs"].items():
        current = {"run_initialization_utc": runs[0] + ":00+00:00", "complete_standard_day": True,
                   "standard_day_start": start.isoformat(), "standard_day_end": end.isoformat(),
                   "forecast_receipt_hash": "forecast", "low_f": 50, "high_f": 72,
                   "dew_point_2m_day_mean": 45, "cloud_cover_day_mean": 20, "wind_speed_10m_day_mean": 6}
        row["forecasts"][model] = {"current": current, "low_revision_f": None, "high_revision_f": None}
    return row


def rows(start, days, hours=(8,)):
    plan = make_plan(date.fromisoformat(start), days, hours, stations=["KPHL"])
    return [example(d) for d in plan["decisions"]], plan


def test_eligibility_run_identity_and_missing_forecasts_fail_closed():
    data, plan = rows("2026-04-03", 2)
    accepted, rejected = validated_rows(data, plan)
    assert len(accepted) == 2 and not rejected
    data[0]["forecasts"]["gfs_global"]["current"]["run_initialization_utc"] = "2026-04-02T12:00:00Z"
    accepted, rejected = validated_rows(data, plan)
    assert len(accepted) == 1 and rejected["ARCHIVE_RUN_OR_WINDOW_MISMATCH"] == 1
    with pytest.raises(ValueError, match="DUPLICATE"):
        validated_rows(data + data, plan)


def test_model_features_do_not_read_outcomes_and_snapshots_share_weight():
    data, _ = rows("2026-04-03", 2)
    before = features(data[0], "low")[0]
    data[0]["observed_low_f"] = -100
    np.testing.assert_array_equal(features(data[0], "low")[0], before)
    assert sum(row_weights(data + [data[0]] * 20)) == pytest.approx(2)


def test_fit_can_learn_bias_and_duplicate_snapshots_do_not_weaken_ridge():
    train, _ = rows("2026-04-03", 20)
    cal, _ = rows("2026-08-31", 12)
    model = fit_metric(train, cal, "low")
    repeated = fit_metric(train + [train[0]] * 20, cal, "low")
    assert predicted_temperature(cal[0], model) == pytest.approx(52)
    np.testing.assert_allclose(model["beta"], repeated["beta"], atol=1e-9)
    assert model["sigma_f"] >= SPEC["sigma_floor_f"]
    metrics = weather_scores(cal, {"low": model})
    assert metrics["low"]["archive_calibrated"]["mae_f"] < metrics["low"]["equal_weight_ensemble"]["mae_f"]


def test_probability_vector_and_exact_market_fallback():
    train, _ = rows("2026-04-03", 20)
    cal, _ = rows("2026-08-31", 12)
    model = fit_metric(train, cal, "low")
    vector = panel()[0]
    vector["archive_probability"] = archived_probabilities(vector, cal[0], model).tolist()
    assert sum(vector["archive_probability"]) == pytest.approx(1)
    np.testing.assert_array_equal(blend(vector, 0), vector["market"])
    assert max(abs(blend(vector, .5) - vector["market"])) <= .05 + 1e-12


def test_partial_archive_is_not_relabelled_as_full_retest(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    plan = make_plan(date(2026, 4, 3), 200)
    (archive / "plan.json").write_text(json.dumps(plan))
    (archive / "latest.json").write_text(json.dumps({"plan_id": plan["plan_id"], "status": "partial_or_blocked", "requests_available": 83}))
    result = run(archive, tmp_path / "missing-market.json", tmp_path / "output")
    assert result["status"] == "awaiting_complete_archive" and result["retest_performed"] is False


def test_market_selection_never_uses_later_test_outcomes():
    from backend.ml.weather_learning.dataset import utc
    from backend.tests.test_weather_market_correction import group
    targets = [date(2026, 9, day) for day in (20, 21, 22)] + [date(2026, 10, day) for day in (3, 4)]
    archive, market = [], []
    for target in targets:
        delta = target - date(2026, 9, 2)
        archive.extend(rows(target.isoformat(), 1)[0])
        for row in group():
            m = row["metadata"]
            row["id"] += target.isoformat()
            for k in ("created_at", "resolved_at"):
                row[k] = (utc(row[k]) + delta).isoformat()
            for k in ("decision_at", "quote_received_at", "weather_forecast_received_at", "observations_received_at", "label_available_at"):
                m[k] = (utc(m[k]) + delta).isoformat()
            m["fee_estimate"]["fee_schedule_checked_at"] = (utc(m["fee_estimate"]["fee_schedule_checked_at"]) + delta).isoformat()
            m["run_id"] += target.isoformat()
            m["target_date"] = m["contract"]["target_date"] = target.isoformat()
            for k in ("window_start", "window_end"):
                m["contract"][k] = (utc(m["contract"][k]) + delta).isoformat()
            market.append(row)
    train, _ = rows("2026-04-03", 20)
    cal, _ = rows("2026-08-31", 12)
    models = {m: fit_metric(train, cal, m) for m in ("low", "high")}
    first = market_replay(archive, market, models, "2026-10-20T00:00:00Z")
    assert first["selected_weight"] > 0
    assert first["test_station_dates"] == 2
    assert first["actual_fills_verified"] is False
    changed = copy.deepcopy(market)
    for row in changed:
        if row["metadata"]["target_date"] >= "2026-10-03":
            row["is_correct"] = not row["is_correct"]
            row["metadata"]["official_result"] = "yes" if row["is_correct"] else "no"
    second = market_replay(archive, changed, models, "2026-10-20T00:00:00Z")
    assert first["selected_weight"] == second["selected_weight"]
    assert second["status"] == "does_not_pass_screen"


def test_complete_archive_retest_is_immutable_and_checks_dataset_hash(tmp_path, monkeypatch):
    import backend.weather_archive_retest as module
    archive = tmp_path / "archive"
    archive.mkdir()
    plan = make_plan(date(2026, 4, 3), 200)
    # One available hour per station/date still gives each date unit weight.
    data = [example(d) for d in plan["decisions"] if d["local_hour"] == 8]
    fingerprint = digest(data)
    (archive / "plan.json").write_text(json.dumps(plan))
    (archive / (fingerprint + "_dataset.json")).write_text(json.dumps(data))
    (archive / "latest.json").write_text(json.dumps({"plan_id": plan["plan_id"], "status": "complete_with_gaps",
        "requests_available": len(plan["requests"]), "dataset_hash": fingerprint, "dataset_file": fingerprint + "_dataset.json"}))
    market = tmp_path / "market.json"
    market.write_text('[]')
    monkeypatch.setattr(module, "market_replay", lambda *a: {"status": "insufficient_matching_market_test"})
    result = run(archive, market, tmp_path / "output")
    assert result["status"] == "offline_retest_complete"
    assert result["coverage"]["train"]["stations"] == 21
    assert result["profitability_proven"] is False
    data[0]["observed_low_f"] += 1
    (archive / (fingerprint + "_dataset.json")).write_text(json.dumps(data))
    with pytest.raises(ValueError, match="DATASET_HASH"):
        run(archive, market, tmp_path / "output")
