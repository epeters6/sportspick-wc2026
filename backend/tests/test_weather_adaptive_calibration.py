import copy
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.ml.weather_learning.dataset import digest
from backend.tests.test_weather_station_distribution import history
from backend.weather_adaptive_calibration import SPEC, fit_day, replay, run, verify_evidence


def errors(start, days):
    return [{"station": s, "target_date": (date.fromisoformat(start) + timedelta(days=i)).isoformat(),
             "label_target_cutoff": (date.fromisoformat(start) + timedelta(days=i - 3)).isoformat(),
             "error_f": 8 if s == "KPHL" else 0}
            for i in range(days) for s in ("KPHL", "KNYC")]


def test_recent_bias_discards_old_regime_and_shrinks_station_means():
    raw = history("2026-06-01", 100)
    cutoff = "2026-08-20"
    for r in raw:
        r["observed_high_f"] = r["forecast_high_f"] + (6 if r["station"] == "KPHL" else -6)
        if r["target_date"] < "2026-07-07" or r["target_date"] > cutoff:
            r["observed_high_f"] += 100
    model = fit_day(raw, [], "2026-08-23", "high", ["KPHL", "KNYC"])
    assert model["label_target_cutoff"] == cutoff
    assert model["pooled_bias_f"] == 0
    assert model["stations"]["KPHL"]["bias_f"] == pytest.approx(4.5)
    assert model["stations"]["KNYC"]["bias_f"] == pytest.approx(-4.5)
    assert model["stations"]["KPHL"]["bias_dates"] == 45
    assert not model["uncertainty_ready"]


def test_width_uses_prior_prediction_errors_even_when_current_fit_has_no_error():
    raw = history("2026-06-01", 100)
    for r in raw:
        r["observed_low_f"] = r["forecast_low_f"]
    earlier = errors("2026-06-22", 60)
    model = fit_day(raw, earlier, "2026-08-23", "low", ["KPHL", "KNYC"])
    assert model["stations"]["KPHL"]["bias_f"] == 0
    assert model["pooled_prequential_mse_f2"] == 32
    assert model["stations"]["KPHL"]["sigma_f"] == pytest.approx(((60 * 64 + 30 * 32) / 90) ** .5)
    assert model["stations"]["KNYC"]["sigma_f"] == pytest.approx((30 * 32 / 90) ** .5)
    for r in earlier:
        r["error_f"] = 0
    floor = fit_day(raw, earlier, "2026-08-23", "low", ["KPHL", "KNYC"])
    assert floor["stations"]["KPHL"]["sigma_f"] == 1.5


def test_maturity_lag_excludes_recent_labels_and_residuals():
    raw = history("2026-06-01", 100)
    earlier = errors("2026-06-22", 65)
    before = fit_day(raw, earlier, "2026-08-23", "low", ["KPHL", "KNYC"])
    for row in raw:
        if row["target_date"] > "2026-08-20":
            row["observed_low_f"] -= 100
    for row in earlier:
        if row["target_date"] > "2026-08-20":
            row["error_f"] += 100
    assert fit_day(raw, earlier, "2026-08-23", "low", ["KPHL", "KNYC"]) == before
    earlier[10]["label_target_cutoff"] = earlier[10]["target_date"]
    with pytest.raises(ValueError, match="ERROR_TIMING_MISMATCH"):
        fit_day(raw, earlier, "2026-08-23", "low", ["KPHL", "KNYC"])


def test_future_label_mutation_cannot_change_earlier_predictions():
    raw = history("2026-04-03", 199)
    before = replay(raw)
    altered = copy.deepcopy(raw)
    for r in altered:
        if r["target_date"] >= "2026-09-25":
            r["observed_high_f"] += 20
    after = replay(altered)
    by_key = lambda evidence: {(p["metric"], p["station"], p["target_date"]): p for p in evidence["predictions"]}
    a, b = by_key(before), by_key(after)
    assert {k: p for k, p in a.items() if k[2] < "2026-09-28"} == {k: p for k, p in b.items() if k[2] < "2026-09-28"}
    assert a["high", "KPHL", "2026-09-28"]["mean_f"] != b["high", "KPHL", "2026-09-28"]["mean_f"]
    for k in a:
        if k[0] == "low":
            assert a[k]["mean_f"] == b[k]["mean_f"]
            assert a[k]["sigma_f"] == b[k]["sigma_f"]


def test_each_error_origin_excludes_its_label_and_target_is_not_in_fit():
    evidence = replay(history("2026-04-03", 199))
    for model in evidence["models"].values():
        assert model["label_target_cutoff"] == (date.fromisoformat(model["target_date"]) - timedelta(days=3)).isoformat()
        assert model["latest_training_target"] <= model["label_target_cutoff"]
        assert model["latest_error_target"] is None or model["latest_error_target"] <= model["label_target_cutoff"]
    for p in evidence["predictions"]:
        model = evidence["models"][p["model_id"]]
        assert model["uncertainty_ready"]
        assert model["stations"][p["station"]]["error_dates"] >= 30


def test_duplicate_events_and_missing_replay_history_fail_closed():
    raw = history("2026-04-03", 199)
    with pytest.raises(ValueError, match="DUPLICATE_STATION_DATE"):
        replay(raw + raw[:1])
    with pytest.raises(ValueError, match="INSUFFICIENT_ROLLING_BIAS_HISTORY"):
        replay(history("2026-09-20", 29))
    with pytest.raises(ValueError, match="INSUFFICIENT_PREQUENTIAL_ERROR_HISTORY"):
        replay(history("2026-08-01", 80))


def test_run_persists_replay_before_scoring_and_does_not_backdate(tmp_path, monkeypatch):
    import backend.weather_adaptive_calibration as module
    source, output = tmp_path / "history", tmp_path / "result"
    source.mkdir()
    raw = history("2026-04-03", 199, tuple(SPEC["stations"]))
    (source / "dataset.json").write_text(json.dumps(raw))
    (source / "latest.json").write_text(json.dumps({"status": "complete", "model": "gfs_global", "dataset_hash": digest(raw)}))
    evaluate = module.evaluate
    def check_order(test, evidence, comparators):
        assert json.loads((output / "spec.json").read_text()) == SPEC
        assert json.loads(next(output.glob("*_replay.json")).read_text()) == evidence
        return evaluate(test, evidence, comparators)
    monkeypatch.setattr(module, "evaluate", check_order)
    started = datetime.now(timezone.utc)
    report = run(source, output)
    assert datetime.fromisoformat(report["created_at"]) >= started
    assert report["coverage"]["test_station_dates"] == 29 * 21
    assert report["coverage"]["missing_station_dates"] == []
    assert report["earlier_evaluation_labels_used_for_later_fits"] is True
    assert report["current_or_future_target_labels_used"] is False
    assert report["deployment_changed"] is False
    assert run(source, output) == report
    path = output / f"{report['artifact_id']}_replay.json"
    evidence = json.loads(path.read_text())
    evidence["predictions"][0]["mean_f"] += 1
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="EVIDENCE_IDENTITY_MISMATCH"):
        run(source, output)


def test_original_comparator_reproduced_without_mutation():
    from backend.weather_station_distribution import fit_metric, split
    raw = history("2026-04-03", 199, tuple(SPEC["stations"]))
    parts = split(raw)
    before = {m: fit_metric(parts["train"], parts["calibration"], m) for m in ("high", "low")}
    replay(raw)
    after = {m: fit_metric(parts["train"], parts["calibration"], m) for m in ("high", "low")}
    assert before == after
