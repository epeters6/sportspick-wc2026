import copy
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.special import ndtr

from backend.ml.weather_learning.dataset import digest
from backend.tests.test_weather_station_distribution import history
from backend.weather_residual_calibration import (
    SPEC, mixture_distribution, mixture_weights, replay, residual_model,
    run, score_gaussian, score_mixture,
)


def residuals(days=30):
    return [{"station": station, "target_date": (date(2026, 8, 22) + timedelta(days=i)).isoformat(),
             "label_target_cutoff": (date(2026, 8, 19) + timedelta(days=i)).isoformat(),
             "parent_model_id": "synthetic", "error_f": 6 if station == "KPHL" else -6}
            for i in range(days) for station in ("KPHL", "KNYC")]


def test_residual_weights_preserve_each_event_and_exact_moments():
    rows = residuals()
    weights = mixture_weights(rows, "KPHL")
    assert weights.sum() == pytest.approx(1)
    assert weights[0] == pytest.approx((1 + 15/60) / 45)
    assert weights[1] == pytest.approx((15/60) / 45)
    model = residual_model(rows, "2026-09-23", "high", ["KPHL", "KNYC"])
    assert model["stations"]["KPHL"]["mean_shift_f"] == pytest.approx(4)
    assert model["stations"]["KNYC"]["mean_shift_f"] == pytest.approx(-4)
    assert model["stations"]["KPHL"]["moment_sigma_f"] == pytest.approx(21 ** .5)


def test_gaussian_kernel_score_reduces_to_gaussian_and_translation_is_invariant():
    kernel = mixture_distribution([2.0], [1.0], 1.5)
    for observed in (-12, 0, 2, 20):
        a = score_mixture(observed, 10, kernel)
        b = score_gaussian(observed, 12, 1.5)
        assert a == pytest.approx(b, abs=1e-9)
        assert score_mixture(observed + 100, 110, kernel) == pytest.approx(a, abs=1e-9)


def test_mixture_crps_matches_cdf_integral_and_quantiles_allow_asymmetry():
    means, weights, sigma = np.array([-6., 0., 3.]), np.array([.1, .7, .2]), 1.
    kernel = mixture_distribution(means, weights, sigma)
    y = 1.3
    cdf = lambda x: float(weights @ ndtr((x - means) / sigma))
    integral = quad(lambda x: cdf(x) ** 2, -30, y)[0] + quad(lambda x: (cdf(x) - 1) ** 2, y, 30)[0]
    assert score_mixture(y, 0, kernel)["crps_f"] == pytest.approx(integral, abs=1e-7)
    lo, hi = kernel["intervals"]["90"]
    assert cdf(lo) == pytest.approx(.05, abs=1e-9)
    assert cdf(hi) == pytest.approx(.95, abs=1e-9)
    assert abs((hi + lo) / 2 - kernel["mean"]) > .1
    far = score_mixture(1000, 0, kernel)
    assert np.isfinite(far["nll"])
    assert far["miss_above_90"] == 1 and far["coverage_90"] == 0
    assert far["interval_score_90_f"] > far["width_90_f"]


@pytest.mark.parametrize("means,weights,sigma", [([], [], 1), ([0], [.5], 1), ([0], [1], 0),
                                                     ([np.nan], [1], 1), ([0, 1], [-1, 2], 1)])
def test_invalid_distribution_fails_closed(means, weights, sigma):
    with pytest.raises(ValueError, match="INVALID_MIXTURE"):
        mixture_distribution(means, weights, sigma)


def test_current_future_and_expired_residuals_excluded_and_lineage_checked():
    rows = residuals(35)
    before = residual_model(rows, "2026-09-23", "low", ["KPHL", "KNYC"])
    for r in rows:
        if r["target_date"] > "2026-09-20":
            r["error_f"] += 100
    rows.append({**rows[0], "target_date": "2026-01-01", "error_f": 999})
    assert residual_model(rows, "2026-09-23", "low", ["KPHL", "KNYC"]) == before
    with pytest.raises(ValueError, match="DUPLICATE_RESIDUAL_EVENT"):
        residual_model(rows + rows[:1], "2026-09-23", "low", ["KPHL", "KNYC"])
    bad = copy.deepcopy(rows)
    bad[0]["label_target_cutoff"] = bad[0]["target_date"]
    with pytest.raises(ValueError, match="RESIDUAL_TARGET_LEAKAGE"):
        residual_model(bad, "2026-09-23", "low", ["KPHL", "KNYC"])
    with pytest.raises(ValueError, match="INSUFFICIENT_STATION_RESIDUALS"):
        residual_model(rows[:20], "2026-09-23", "low", ["KPHL", "KNYC"])


def test_future_label_changes_only_later_eligible_predictions():
    rows = history("2026-04-03", 199)
    before = replay(rows)
    for r in rows:
        if r["target_date"] >= "2026-09-25":
            r["observed_high_f"] += 10
    after = replay(rows)
    keyed = lambda obj: {(p["metric"], p["station"], p["target_date"]): p for p in obj["predictions"]}
    a, b = keyed(before), keyed(after)
    assert {k: p for k, p in a.items() if k[2] < "2026-09-28"} == {k: p for k, p in b.items() if k[2] < "2026-09-28"}
    assert a["high", "KPHL", "2026-09-28"]["mean_f"] != b["high", "KPHL", "2026-09-28"]["mean_f"]
    for k in a:
        if k[0] == "low":
            assert a[k]["mean_f"] == b[k]["mean_f"] and a[k]["sigma_f"] == b[k]["sigma_f"]


def test_full_run_saves_evidence_before_scores_and_rejects_tampering(tmp_path, monkeypatch):
    import backend.weather_residual_calibration as module
    source, output = tmp_path / "history", tmp_path / "output"
    source.mkdir()
    rows = history("2026-04-03", 199, tuple(SPEC["stations"]))
    (source / "dataset.json").write_text(json.dumps(rows))
    (source / "latest.json").write_text(json.dumps({"status": "complete", "model": "gfs_global", "dataset_hash": digest(rows)}))
    evaluate = module.evaluate
    def checked_evaluate(test, evidence, raw_models):
        assert json.loads((output / "spec.json").read_text()) == SPEC
        assert json.loads(next(output.glob("*_replay.json")).read_text()) == evidence
        return evaluate(test, evidence, raw_models)
    monkeypatch.setattr(module, "evaluate", checked_evaluate)
    start = datetime.now(timezone.utc)
    report = run(source, output)
    assert datetime.fromisoformat(report["created_at"]) >= start
    assert report["coverage"]["test_station_dates"] == 609
    assert report["deployment_changed"] is False
    assert report["earlier_evaluation_labels_used_for_later_fits"] is True
    assert run(source, output) == report
    path = output / f"{report['artifact_id']}_replay.json"
    artifact = json.loads(path.read_text())
    artifact["predictions"][0]["mean_f"] += 1
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="EVIDENCE_IDENTITY_MISMATCH"):
        run(source, output)
