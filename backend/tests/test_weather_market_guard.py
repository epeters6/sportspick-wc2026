import copy
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from backend.ml.weather_learning.dataset import digest, utc
from backend.tests.test_weather_probability_screen import market_group
from backend.weather_market_guard import SPEC, blend, fit, load_model, predict_forward, save_model, verify
from backend.weather_model_diagnostics import diagnose, forward_panel, summarize


def history(*, bad=True, days=12, stations=10):
    rows = []
    for day in range(days):
        for station in range(stations):
            for row in market_group():
                m = row["metadata"]
                shift = timedelta(days=day)
                for key in ("created_at", "resolved_at"):
                    row[key] = (utc(row[key])+shift).isoformat()
                for key in ("decision_at", "quote_received_at", "weather_forecast_received_at", "observations_received_at", "label_available_at"):
                    m[key] = (utc(m[key])+shift).isoformat()
                m["target_date"] = (datetime.fromisoformat(m["target_date"])+shift).date().isoformat()
                m.update(station=f"TEST{station}", run_id=f"{day}-{station}", model_id="saved-model",
                         model_trained_at="2026-08-01T00:00:00Z")
                m["contract"]["window_end"] = (utc(m["contract"]["window_end"])+shift).isoformat()
                p = (.9 if not row["is_correct"] else .1) if bad else (.1 if not row["is_correct"] else .9)
                m["challenger_probabilities"] = {"regularized_logistic": p, "boosted_trees": p}
                row["id"] += f"-{day}-{station}"
                rows.append(row)
    return rows


def test_bad_forecasts_fall_back_exactly_to_market():
    model = fit(history(), as_of="2026-10-01T00:00:00Z")
    assert model["selected"] == "market" and model["weight"] == 0
    np.testing.assert_array_equal(blend([0, 1], [1, 0], 0), [0, 1])


def test_small_sample_cannot_promote_and_future_labels_cannot_change_selection():
    assert fit(history(days=2), as_of="2026-10-01T00:00:00Z")["reason"] == "insufficient_calibration"
    before = fit(history(), as_of="2026-10-01T00:00:00Z")
    later = history(bad=False)
    for row in later:
        row["id"] += "future"
        row["metadata"]["run_id"] += "future"
        row["resolved_at"] = row["metadata"]["label_available_at"] = "2027-01-01T00:00:00Z"
    after = fit(history()+later, as_of="2026-10-01T00:00:00Z")
    assert before["candidates"] == after["candidates"]


def test_positive_adjustments_stay_bounded_and_require_both_scores():
    model = fit(history(bad=False), as_of="2026-10-01T00:00:00Z")
    assert model["weight"] > 0
    p = blend([.5, .5], [.1, .9], model["weight"])
    assert max(abs(p-np.array([.5,.5]))) <= .05+1e-12
    assert sum(p) == pytest.approx(1)
    for c in model["candidates"]:
        assert c["scores"]["brier"] < model["market_scores"]["brier"]
        assert c["scores"]["log_loss"] < model["market_scores"]["log_loss"]


def test_diagnostic_brier_decomposition_and_station_date_weighting():
    panel, _ = forward_panel(history(), "2026-10-01T00:00:00Z")
    expected = summarize(panel)
    other_metric = copy.deepcopy(panel[0])
    other_metric["metric"], other_metric["venue"] = "high", "polymarket"
    assert summarize(panel+[other_metric]*40)["scores"] == expected["scores"]
    for value in expected["scores"].values():
        assert value["brier_change_vs_market"] == pytest.approx(value["directional_term"]+value["adjustment_penalty"])
    # Different event difficulty exposes accidental snapshot weighting.
    easy = copy.deepcopy(panel[0])
    hard = copy.deepcopy(panel[1])
    easy["market"] = [float(r["is_correct"]) for r in easy["rows"]]
    hard["market"] = [float(not r["is_correct"]) for r in hard["rows"]]
    assert summarize([easy, hard])["scores"]["market"]["brier"] == 1.0
    assert summarize([easy]*40+[hard])["scores"]["market"]["brier"] == 1.0


@pytest.mark.parametrize("change", ["future_model", "future_input", "invalid_probability", "mixed_model"])
def test_bad_forward_evidence_is_excluded(change):
    rows = history(days=1, stations=1)
    m = rows[0]["metadata"]
    if change == "future_model":
        for row in rows: row["metadata"]["model_trained_at"] = "2027-01-01T00:00:00Z"
    if change == "future_input": m["quote_received_at"] = "2027-01-01T00:00:00Z"
    if change == "invalid_probability": m["challenger_probabilities"]["boosted_trees"] = 1.2
    if change == "mixed_model": m["model_id"] = "other"
    panel, _ = forward_panel(rows, "2026-10-01T00:00:00Z")
    assert not panel


def test_artifact_identity_and_no_backdated_forward_forecasts(tmp_path):
    model = fit(history(), as_of="2026-10-01T00:00:00Z")
    save_model(model, tmp_path)
    assert load_model(tmp_path) == model
    old = history(days=1, stations=1)
    for row in old: row["is_correct"] = row["resolved_at"] = None
    with pytest.raises(ValueError, match="NOT_AVAILABLE_BEFORE"):
        predict_forward(old, model, tmp_path)
    changed = copy.deepcopy(model)
    changed["weight"] = .5
    with pytest.raises(ValueError, match="IDENTITY"):
        verify(changed)


def test_future_unlabelled_capture_saves_separate_immutable_forecast(tmp_path):
    model = fit(history(), as_of="2026-10-01T00:00:00Z")
    raw = history(days=1, stations=1)
    decision = utc(model["created_at"])+timedelta(seconds=1)
    for row in raw:
        row["resolved_at"] = row["is_correct"] = None
        m = row["metadata"]
        m["decision_at"] = row["created_at"] = decision.isoformat()
        m["target_date"] = (decision+timedelta(days=1)).date().isoformat()
        m["contract"]["window_end"] = (decision+timedelta(days=2)).isoformat()
    original = copy.deepcopy(raw)
    result = predict_forward(raw, model, tmp_path, as_of=decision+timedelta(seconds=1))
    assert raw == original and result["forecast_vectors"] == 1
    assert len(list((tmp_path/'forecasts').glob('*.json'))) == 1
    again = predict_forward(raw, model, tmp_path, as_of=decision+timedelta(seconds=2))
    assert again["fingerprints"] == result["fingerprints"]
    assert len(list((tmp_path/'forecasts').glob('*.json'))) == 1
    with pytest.raises(ValueError, match="AFTER_OBSERVATION_WINDOW"):
        predict_forward(raw, model, tmp_path, as_of=decision+timedelta(days=3))


def test_nonfinite_evidence_fails_closed():
    raw = history(days=1, stations=1)
    raw[0]["metadata"]["challenger_probabilities"]["boosted_trees"] = float('nan')
    with pytest.raises(ValueError):
        fit(raw, as_of="2026-10-01T00:00:00Z")
