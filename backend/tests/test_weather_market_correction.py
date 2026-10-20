import copy
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from backend.ml.weather_learning.dataset import digest
from backend.tests.test_weather_probability_screen import market_group
from backend.weather_market_correction import (
    FEATURE_NAMES, SPEC, chronological_parts, cost_diagnostics, event_key, fit_select,
    implementation_hash, make_panel, predict_forward, probabilities, scores,
)

NOW = "2026-10-20T20:00:00Z"


def group():
    rows = market_group()
    for i, row in enumerate(rows):
        m = row["metadata"]
        m.update(weather_forecast_hash="forecast1", observations_hash="observations")
        m["weather_features"] = {"gfs_low_f": 50, "hrrr_low_f": 51, "ecmwf_low_f": 52,
                                  "nowcast_probability": [0.4, 0.6][i], "prior_sigma_f": 3,
                                  "remaining_hours": 24, "observation_age_minutes": None,
                                  "model_disagreement_f": 1}
        m["ask_depth"] = 2
    return rows


def panel():
    p, q = make_panel(group(), NOW)
    assert q["accepted_vectors"] == 1
    return p


def test_panel_keeps_bucket_specific_probability_and_distinguishes_run_age():
    p = panel()
    assert p[0]["x"][0][3] != p[0]["x"][1][3]
    assert p[0]["forecast_run_age_verified"] is False
    assert len(p[0]["x"][0]) == len(FEATURE_NAMES)


def test_revision_uses_earlier_input_without_requiring_its_label():
    earlier = group()
    for row in earlier:
        row["id"] += "earlier"
        row["resolved_at"], row["is_correct"] = None, None
        m = row["metadata"]
        m.update(run_id="earlier", decision_at="2026-09-01T09:10:00Z", weather_forecast_received_at="2026-09-01T09:00:00Z",
                 observations_received_at="2026-09-01T09:00:00Z", weather_forecast_hash="earlier")
        m["weather_features"]["gfs_low_f"] = 48
    p, _ = make_panel(group() + earlier, NOW)
    assert p[0]["previous_weather"]["gfs"] == 48
    assert p[0]["previous_weather_known_at"] == "2026-09-01T09:10:00+00:00"
    later = copy.deepcopy(earlier)
    for row in later:
        row["metadata"]["decision_at"] = "2026-09-01T13:10:00Z"
        row["metadata"]["weather_forecast_received_at"] = "2026-09-01T13:00:00Z"
    p, _ = make_panel(group() + later, NOW)
    assert p[0]["previous_weather"] is None


def test_conflicting_receipt_is_not_silently_replaced():
    rows = group()
    rows[1]["metadata"]["weather_features"]["gfs_low_f"] = 55
    with pytest.raises(ValueError, match="CONFLICTING_WEATHER_RECEIPT_VALUES"):
        make_panel(rows, NOW)


def test_no_future_inputs_or_invalid_quote_enter_panel():
    rows = group()
    rows[0]["metadata"]["quote_received_at"] = "2026-09-01T12:11:00Z"
    p, q = make_panel(rows, NOW)
    assert not p and q["rejected_vectors"]["STALE_OR_FUTURE_QUOTE"] == 1
    rows = group()
    for row in rows:
        row["metadata"]["weather_forecast_received_at"] = "2026-09-01T13:00:00Z"
    p, _ = make_panel(rows, NOW)
    assert not p


def test_exact_market_fallback_and_bounded_probability_correction():
    v = panel()[0]
    np.testing.assert_array_equal(probabilities(v, [100] * len(FEATURE_NAMES), 0), v["market"])
    result = probabilities(v, [100] * len(FEATURE_NAMES), 1)
    assert sum(result) == pytest.approx(1)
    assert max(abs(result - v["market"])) <= SPEC["maximum_bucket_probability_change"] + 1e-12


def test_high_low_and_venues_share_station_date_weight():
    a = panel()[0]
    b = copy.deepcopy(a)
    b["station"] = "KORD"
    for row in b["rows"]:
        row["is_correct"] = not row["is_correct"]
    a["market"], b["market"] = [0.2, 0.8], [0.2, 0.8]
    assert scores([a, b]) == scores([a] * 50 + [b])
    low = copy.deepcopy(a)
    low["metric"] = "high"
    assert event_key(a) == event_key(low)


def test_calibration_can_choose_zero_and_does_not_receive_test_rows(monkeypatch):
    import backend.weather_market_correction as module
    monkeypatch.setattr(module, "fit_offset", lambda v, r: [0.0] * len(FEATURE_NAMES))
    chosen, report = fit_select(panel(), panel())
    assert chosen["weight"] == 0
    assert report["selection_used_test_outcomes"] is False


def test_cost_sensitivity_requires_depth_fees_and_keeps_fill_claim_false():
    p = panel()
    p[0]["market"] = [0.1, 0.9]
    report = cost_diagnostics(p, [0.0] * len(FEATURE_NAMES), 0)
    assert report["additional_slippage_scenarios"]["0.0"]["station_date_events"] == 1
    assert report["additional_slippage_scenarios"]["0.03"]["hypothetical_unit_pnl"] == pytest.approx(0.47)
    assert report["fills_verified"] is False
    for row in p[0]["rows"]:
        row["metadata"]["ask_depth"] = 1
    assert cost_diagnostics(p, [0] * len(FEATURE_NAMES), 0)["additional_slippage_scenarios"]["0.0"]["station_date_events"] == 0
    p = panel()
    for row in p[0]["rows"]:
        row["metadata"]["fee_estimate"]["fee_schedule_checked_at"] = None
    assert cost_diagnostics(p, [0] * len(FEATURE_NAMES), 0)["rejected_rows"] == {"MISSING_FEE_TIMESTAMP": 2}


def test_forward_predictions_reject_model_created_after_decision_and_tampering():
    p = panel()
    model = {"spec_hash": digest(SPEC), "implementation_hash": implementation_hash(), "created_at": NOW,
             "training_last_target": "2026-09-01", "beta": [0] * len(FEATURE_NAMES), "weight": 0}
    model["model_id"] = digest(model)
    with pytest.raises(ValueError, match="MODEL_NOT_AVAILABLE_BEFORE_FORWARD_DECISION"):
        predict_forward(p, model)
    model["created_at"] = "2026-08-31T00:00:00Z"
    with pytest.raises(ValueError, match="ARTIFACT_IDENTITY_MISMATCH"):
        predict_forward(p, model)


def test_chronological_split_embargoes_future_labels():
    vectors = []
    for day in range(16):
        v = copy.deepcopy(panel()[0])
        decision = datetime(2026, 9, 1, 12, tzinfo=timezone.utc) + timedelta(days=day)
        target = (decision + timedelta(days=1)).date().isoformat()
        v.update(decision_at=decision.isoformat(), target_date=target)
        for row in v["rows"]:
            row["id"] += str(day)
            row["metadata"].update(decision_at=decision.isoformat(), target_date=target, run_id=str(day))
            row["resolved_at"] = (decision + timedelta(days=2)).isoformat()
        vectors.append(v)
    train, cal, test = chronological_parts(vectors)
    assert max(r["resolved_at"] for v in train for r in v["rows"]) < min(v["decision_at"] for v in cal)
    assert max(r["resolved_at"] for v in cal for r in v["rows"]) < min(v["decision_at"] for v in test)


def test_saved_artifact_creation_cannot_be_backdated_with_evaluation_cutoff(tmp_path, monkeypatch):
    import backend.weather_market_correction as module
    rows = []
    for day in range(16):
        for row in group():
            m = row["metadata"]
            shift = timedelta(days=day)
            row["id"] += str(day)
            for key in ("created_at", "resolved_at"):
                row[key] = (module.utc(row[key]) + shift).isoformat()
            for key in ("decision_at", "quote_received_at", "weather_forecast_received_at", "observations_received_at", "label_available_at"):
                m[key] = (module.utc(m[key]) + shift).isoformat()
            m["run_id"] += str(day)
            m["target_date"] = (datetime.fromisoformat(m["target_date"]) + shift).date().isoformat()
            m["contract"]["target_date"] = m["target_date"]
            for key in ("window_start", "window_end"):
                m["contract"][key] = (module.utc(m["contract"][key]) + shift).isoformat()
            rows.append(row)
    source = tmp_path / "input.json"
    source.write_text(json.dumps(rows))
    monkeypatch.setattr(module, "fit_offset", lambda v, r: [0.0] * len(FEATURE_NAMES))
    started = datetime.now(timezone.utc)
    result = module.run(source, tmp_path / "out", as_of="2026-09-20T00:00:00Z")
    model = json.loads((tmp_path / "out" / "models" / (result["model_id"] + ".json")).read_text())
    assert module.utc(model["created_at"]) >= started
    assert result["status"] == "market_fallback_selected"
    assert result["forward_predictions_created"] == 0
    module.verify_artifact(model)
