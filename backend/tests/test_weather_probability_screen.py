import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.ml.weather_learning.dataset import digest
from backend.weather_historical_calibration import standard_day
from backend.weather_probability_screen import (
    SPEC, advancement_decisions, fit, probability_vector, quote_diagnostic, replay, run, summarize, uncertainty,
    validate_history, verify_model, write_once,
)

NOW = "2026-10-20T20:00:00Z"


def history_row(target, *, low=50):
    day = date.fromisoformat(target)
    start, end = standard_day("KPHL", day)
    return {"station": "KPHL", "target_date": target, "model": "gfs_global",
            "forecast_low_f": 50, "observed_low_f": low,
            "run_initialization_utc": (datetime.combine(day, datetime.min.time(), timezone.utc) - timedelta(days=1)).isoformat(),
            "standard_day_start": start.isoformat(), "standard_day_end": end.isoformat(),
            "forecast_receipt_hash": "forecast", "label_receipt_hash": "label", "label_product": "CLI",
            "label_link": "https://mesonet.agron.iastate.edu/CLI", "as_of_eligible": False,
            "production_training_eligible": False, "retrieved_at": "2026-10-19T00:00:00Z"}


def history_rows():
    return [history_row((date(2026, 4, 3) + timedelta(days=i)).isoformat(), low=52) for i in range(120)]


def market_group():
    rows = []
    for i, (lo, hi, winner) in enumerate([(None, 51.5, False), (51.5, None, True)]):
        m = {"station": "KPHL", "station_verified": True, "target_date": "2026-09-02", "metric": "low",
             "run_id": "run", "platform": "kalshi", "decision_at": "2026-09-01T12:10:00Z",
             "observation_timezone": "Etc/GMT+5", "lead_days": 1, "raw_model_prob": 0.5,
             "bucket_low_f": lo, "bucket_high_f": hi, "market_vector_prob": 0.5,
             "label_source": "venue_official", "official_market_id": str(i),
             "official_result": "yes" if winner else "no", "label_available_at": "2026-09-03T12:00:00Z",
             "quote_received_at": "2026-09-01T12:09:30Z", "weather_forecast_received_at": "2026-09-01T12:00:00Z",
             "observations_received_at": "2026-09-01T12:00:00Z", "settlement_source": "The Weather Company",
             "executable_cost": 0.5, "ask_depth": 1,
             "fee_estimate": {"fee_per_share": 0.01, "fee_source": "official",
                              "fee_schedule_checked_at": "2026-09-01T12:09:00Z"},
             "contract": {"station": "KPHL", "target_date": "2026-09-02", "metric": "low",
                          "market_id": str(i), "platform": "kalshi", "authority": "The Weather Company",
                          "window_start": "2026-09-02T05:00:00Z", "window_end": "2026-09-03T05:00:00Z",
                          "low_f": lo, "high_f": hi}}
        rows.append({"id": str(i), "outcome": str(i), "source": "weather_station_learning_v3",
                     "prob": 0.5, "created_at": m["decision_at"], "resolved_at": m["label_available_at"],
                     "is_correct": winner, "metadata": m})
    return rows


def test_training_cutoff_excludes_later_labels_and_never_backdates_model():
    rows = history_rows()
    later = history_row("2026-09-02", low=200)
    a = fit(rows, target_end=SPEC["training_target_end"], created_at=NOW)
    b = fit(rows + [later], target_end=SPEC["training_target_end"], created_at=NOW)
    assert a == b
    assert a["created_at"].startswith("2026-10-20")
    assert a["residuals_f"]["KPHL"] == [2] * 120
    with pytest.raises(ValueError, match="RECEIVED_AFTER"):
        fit(rows, target_end=SPEC["training_target_end"], created_at="2026-09-01T00:00:00Z")


def test_history_hash_identity_and_exact_run_are_required():
    row = history_row("2026-09-02")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        validate_history([row], "bad")
    assert validate_history([row], digest([row]))[("KPHL", "2026-09-02")] == row
    row["run_initialization_utc"] = "2026-09-01T06:00:00Z"
    with pytest.raises(ValueError, match="RUN_OR_WINDOW"):
        validate_history([row], digest([row]))


def test_duplicate_dates_and_changed_training_eligibility_fail_closed():
    row = history_row("2026-09-02")
    with pytest.raises(ValueError, match="DUPLICATE_STATION_DATE"):
        validate_history([row, row], digest([row, row]))
    row["production_training_eligible"] = True
    with pytest.raises(ValueError, match="ELIGIBILITY_CHANGED"):
        validate_history([row], digest([row]))


def test_probability_mass_and_bias_are_correct_without_missing_bucket_renormalization():
    model = fit(history_rows(), target_end=SPEC["training_target_end"], created_at=NOW)
    group = market_group()
    values = probability_vector(group, 50, model)
    assert sum(values) == pytest.approx(1)
    assert values[1] == pytest.approx(0.6914624613)
    with pytest.raises(ValueError, match="INCOMPLETE_PARTITION"):
        probability_vector(group[:1], 50, model)
    model["residuals_f"]["KPHL"][0] = 100
    with pytest.raises(ValueError, match="MODEL_ID"):
        verify_model(model)


def test_replay_uses_official_winner_and_keeps_retrospective_evidence_separate():
    rows = history_rows() + [history_row("2026-09-02", low=50)]
    model = fit(rows, target_end=SPEC["training_target_end"], created_at=NOW)
    result = replay(validate_history(rows, digest(rows)), market_group(), model, as_of=NOW)
    assert result["station_date_events"] == 1
    assert result["cli_venue_disagreement_events"] == 1
    assert result["scores"]["station_residual"]["brier"] < result["scores"]["market"]["brier"]
    assert result["forward_forecasts_created"] == 0
    assert result["historical_as_of_verified"] is False
    assert result["profitability_proven"] is False


@pytest.mark.parametrize("field,value,reason", [
    ("quote_received_at", "2026-09-01T12:11:00Z", "STALE_OR_FUTURE_QUOTE"),
    ("quote_received_at", "2026-09-01T11:00:00Z", "STALE_OR_FUTURE_QUOTE"),
    ("decision_at", "2026-09-02T12:10:00Z", "NOT_NEXT_DAY_ELIGIBLE_DECISION"),
])
def test_replay_rejects_wrong_decision_or_quote_timing(field, value, reason):
    rows = history_rows() + [history_row("2026-09-02")]
    group = market_group()
    for row in group:
        row["metadata"][field] = value
    result = replay(validate_history(rows, digest(rows)), group,
                    fit(rows, target_end=SPEC["training_target_end"], created_at=NOW), as_of=NOW)
    assert result["station_date_events"] == 0
    assert result["rejected_vectors"][reason] == 1


def test_replay_rejects_contract_and_training_overlap():
    rows = history_rows() + [history_row("2026-09-02")]
    model = fit(rows, target_end=SPEC["training_target_end"], created_at=NOW)
    group = market_group()
    group[0]["metadata"]["contract"]["window_start"] = "2026-09-02T04:00:00Z"
    result = replay(validate_history(rows, digest(rows)), group, model, as_of=NOW)
    assert result["rejected_vectors"]["CONTRACT_PROVENANCE_MISMATCH"] == 1
    overlapping = fit(rows, target_end="2026-09-02", created_at=NOW)
    with pytest.raises(ValueError, match="TRAIN_TEST_DATE_OVERLAP"):
        replay(validate_history(rows, digest(rows)), group, overlapping, as_of=NOW)


def test_repeat_snapshots_do_not_multiply_station_date_weight():
    def vector(station, value):
        return {"station": station, "target_date": "2026-09-02", "scores": {
            name: {"brier": value, "log_loss": value} for name in ("market", *SPEC["candidates"])}}
    one, two = vector("KPHL", 0), vector("KORD", 1)
    assert summarize([one, two]) == summarize([one] * 100 + [two])
    assert summarize([one, two])["market"]["brier"] == 0.5
    assert uncertainty([one] * 100, "station_residual")["status"] == "insufficient_calendar_blocks"


def test_a_better_average_or_one_good_metric_never_automatically_promotes():
    summary = {"market": {"brier": 0.5, "log_loss": 0.8},
               "station_residual": {"brier": 0.4, "log_loss": 0.9},
               "half_market_half_residual": {"brier": 0.4, "log_loss": 0.7}}
    decisions = advancement_decisions(summary)
    assert decisions["station_residual"] == "does_not_pass_screen"
    assert decisions["half_market_half_residual"] == "requires_new_frozen_prospective_trial_no_automatic_promotion"


def test_quote_diagnostics_need_depth_and_recent_fee_evidence():
    group = market_group()
    now = datetime(2026, 9, 1, 12, 10, tzinfo=timezone.utc)
    assert quote_diagnostic(group, [0.2, 0.8], now)["outcome"] == "1"
    group[1]["metadata"]["ask_depth"] = 0
    assert quote_diagnostic(group, [0.2, 0.8], now) is None
    group[1]["metadata"]["ask_depth"] = 1
    group[1]["metadata"]["fee_estimate"]["fee_schedule_checked_at"] = "2026-09-01T11:00:00Z"
    assert quote_diagnostic(group, [0.2, 0.8], now) is None


def test_output_is_immutable_and_full_run_stays_offline(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: pytest.fail("network is forbidden"))
    rows = history_rows() + [history_row("2026-09-02")]
    history = tmp_path / "history.json"
    market = tmp_path / "market.json"
    history.write_text(json.dumps(rows))
    market.write_text(json.dumps({"v3": market_group()}))
    result = run(history, market, tmp_path / "result", expected_history_hash=digest(rows), as_of=NOW)
    assert result["station_date_events"] == 1
    assert len(list((tmp_path / "result").glob("*_report.json"))) == 1
    assert run(history, market, tmp_path / "result", expected_history_hash=digest(rows), as_of=NOW) == result
    spec_path = tmp_path / "result" / "spec.json"
    before = spec_path.read_bytes()
    with pytest.raises(ValueError, match="IMMUTABLE_SCREEN_FILE_CONFLICT"):
        write_once(spec_path, {"changed": True})
    assert spec_path.read_bytes() == before
