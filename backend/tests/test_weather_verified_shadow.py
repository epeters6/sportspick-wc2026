import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from backend import weather_daytime_paper as paper
from backend import weather_verified_runs as runs
from backend import weather_verified_shadow as s
from backend import weather_verified_collection as collector
from backend.ml.weather_learning.dataset import digest
from backend.tests.test_weather_daytime_paper import MemoryDB, inputs, official_receipts
from backend.weather_historical_calibration import ARCHIVE, standard_day

NOW = datetime(2026, 10, 22, 12, 5, tzinfo=timezone.utc)
RUN = NOW.replace(hour=6, minute=0)


@pytest.fixture
def model():
    return json.loads((Path(__file__).resolve().parents[2]/"research/weather_daytime_paper/model.json").read_text())


def metadata(name="gfs_global", now=NOW, run=RUN):
    data = {"last_run_initialisation_time": int(run.timestamp()),
            "last_run_availability_time": int((now-timedelta(minutes=30)).timestamp())}
    return {"url": runs.metadata_url(name), "params": {}, "received_at": (now-timedelta(minutes=1)).isoformat(),
            "data": data, "content_hash": digest(data)}


def receipt(model, run=RUN, name="gfs_global", high=77., received=None, target="2026-10-23", station="KPHL"):
    loc = model["locations"][station]
    start, _ = standard_day(station, date.fromisoformat(target))
    data = {"latitude": loc["lat"], "longitude": loc["lon"], "elevation": loc["elev"],
            "utc_offset_seconds": 0, "hourly_units": {"temperature_2m": "°F"},
            "hourly": {"time": [int(start.timestamp())+i*3600 for i in range(24)], "temperature_2m": [high]*24}}
    return {"url": ARCHIVE, "params": runs.parameters(name, run, loc), "received_at": (received or NOW-timedelta(minutes=1)).isoformat(),
            "data": data, "content_hash": digest(data)}


def event(model):
    cs, qs, _ = inputs(model)
    baseline = receipt(model, RUN-timedelta(hours=6), high=75.)
    return s.decision(model, cs, qs, baseline, {"gfs_global": (receipt(model), baseline)},
                      {"gfs_global": metadata()}, {}, NOW)


@pytest.fixture
def store(model, tmp_path):
    db = MemoryDB()
    db.values["weather_daytime_paper_v1:book"] = {"must": "remain untouched"}
    st = s.Store(db, model, tmp_path)
    st.initialize(NOW-timedelta(minutes=10))
    return st


def test_exact_run_revision_not_snapshot_refresh(model):
    new = receipt(model, high=77, received=NOW-timedelta(minutes=2))
    old = receipt(model, RUN-timedelta(hours=6), high=75, received=NOW-timedelta(minutes=1))
    pair = runs.verified_pair(new, old, model="gfs_global", runs=(RUN, RUN-timedelta(hours=6)), station="KPHL",
        target="2026-10-23", location=model["locations"]["KPHL"], decision_at=NOW)
    assert pair["high_revision_f"] == 2 and pair["first_seen_order_required"] is False
    assert pair["current"]["run_age_hours"] > 6
    assert pair["current"]["receipt_age_seconds"] == 120
    assert pair["current"]["exact_publication_time_verified"] is False
    with pytest.raises(ValueError, match="DISTINCT"):
        runs.verified_pair(new, new, model="gfs_global", runs=(RUN, RUN), station="KPHL",
            target="2026-10-23", location=model["locations"]["KPHL"], decision_at=NOW)


def test_gfs_temperature_uses_primary_domain_metadata():
    assert runs.metadata_url("gfs_global") == "https://api.open-meteo.com/data/ncep_gfs013/static/meta.json"
    wrong = metadata()
    wrong["url"] = "https://api.open-meteo.com/data/ncep_gfs025/static/meta.json"
    with pytest.raises(ValueError, match="IDENTITY"): runs.select_pair("gfs_global", wrong, NOW)


@pytest.mark.parametrize("bad", ["future_receipt", "wrong_run", "wrong_model", "wrong_grid", "wrong_units", "missing_hour",
                                      "null_hour", "duplicate_hour", "unsorted_hour", "hash", "naive_time", "late_decision"])
def test_bad_exact_run_evidence_is_rejected(model, bad):
    r, now = receipt(model), NOW
    if bad == "future_receipt": r["received_at"] = (NOW+timedelta(seconds=1)).isoformat()
    if bad == "wrong_run": r["params"]["run"] = "2026-10-22T00:00"
    if bad == "wrong_model": r["params"]["models"] = "best_match"
    if bad == "wrong_grid": r["data"]["latitude"] += 1
    if bad == "wrong_units": r["data"]["hourly_units"]["temperature_2m"] = "°C"
    if bad == "missing_hour": r["data"]["hourly"]["time"].pop(); r["data"]["hourly"]["temperature_2m"].pop()
    if bad == "null_hour": r["data"]["hourly"]["temperature_2m"][0] = None
    if bad == "duplicate_hour": r["data"]["hourly"]["time"][0] = r["data"]["hourly"]["time"][1]
    if bad == "unsorted_hour": r["data"]["hourly"]["time"].reverse()
    if bad == "naive_time": r["received_at"] = "2026-10-22T12:00:00"
    if bad == "late_decision": now += timedelta(days=2)
    if bad != "hash": r["content_hash"] = digest(r["data"])
    else: r["content_hash"] = "invalid"
    with pytest.raises(ValueError):
        runs.verify_run(r, model="gfs_global", run=RUN, station="KPHL", target="2026-10-23",
                        location=model["locations"]["KPHL"], decision_at=now)


@pytest.mark.parametrize("bad", ["missing_availability", "future_availability", "propagation", "stale", "cycle", "domain", "hash"])
def test_initialization_alone_is_not_availability(bad):
    m = metadata()
    if bad == "missing_availability": m["data"].pop("last_run_availability_time")
    if bad == "future_availability": m["data"]["last_run_availability_time"] = int((NOW+timedelta(hours=1)).timestamp())
    if bad == "propagation": m["data"]["last_run_availability_time"] = int((NOW-timedelta(minutes=2)).timestamp())
    if bad == "stale": m["received_at"] = (NOW-timedelta(hours=1)).isoformat()
    if bad == "cycle": m["data"]["last_run_initialisation_time"] += 3600
    if bad == "domain": m["url"] = runs.metadata_url("ecmwf_ifs025")
    if bad != "hash": m["content_hash"] = digest(m["data"])
    else: m["content_hash"] = "bad"
    with pytest.raises(ValueError): runs.select_pair("gfs_global", m, NOW)


def test_extended_hrrr_cycle_and_dst_standard_day(model):
    assert runs.select_pair("gfs_hrrr", metadata("gfs_hrrr", run=RUN+timedelta(hours=3)), NOW) == (RUN, RUN-timedelta(hours=6))
    target, now = "2026-11-01", datetime(2026, 10, 31, 15, tzinfo=timezone.utc)
    run = now.replace(hour=6)
    r = receipt(model, run, received=now-timedelta(seconds=1), target=target)
    info = runs.verify_run(r, model="gfs_global", run=run, station="KPHL", target=target,
                           location=model["locations"]["KPHL"], decision_at=now)
    assert (runs.timestamp(info["standard_day_end"])-runs.timestamp(info["standard_day_start"])).total_seconds() == 86400


def test_comparison_changes_only_forecast_run_and_never_executes(model):
    e = event(model)
    assert e["fresh_mean_f"] - e["mean_f"] == pytest.approx(2)
    assert set(e["vectors"]) == set(s.ARMS)
    assert e["vectors"]["market"] == e["vectors"]["guard_market_fallback"]
    assert e["vectors"]["fresh_gfs_ablation"] != e["vectors"]["frozen_recipe_at_shadow_time"]
    assert all(sum(v) == pytest.approx(1) for v in e["vectors"].values())
    assert e["position"] is None and e["paper_execution_enabled"] is False and e["orders_submitted"] is False
    assert e["source"] != paper.SOURCE and "forecast_receipt" not in e
    assert e["model_id"] == model["model_id"] == s.PARENT
    assert e["quote_only_net_edges"]["market"] == pytest.approx([.285, .285])


def test_store_missing_state_no_reset_and_pending_recovery(store, model):
    untouched = copy.deepcopy(store.db.values["weather_daytime_paper_v1:book"])
    before = store.load()
    pending = store.update(before, s.reserve_event(before, event(model)))
    store.db.fail_write = True
    with pytest.raises(RuntimeError): store.flush(pending)
    store.db.fail_write = False
    state = store.flush(store.load())
    assert state["pending"] is None and len(state["events"]) == 1
    assert store.initialize(NOW) == state
    with pytest.raises(ValueError, match="CONCURRENT"): store.update(before, pending)
    with pytest.raises(ValueError, match="DUPLICATE"): s.reserve_event(state, event(model))
    assert store.db.values["weather_daytime_paper_v1:book"] == untouched
    del store.db.values[store.key("state")]
    with pytest.raises(ValueError, match="MISSING"): store.load()


def test_settlement_same_market_baseline_and_station_day_weight(store, model):
    e = event(model)
    state = store.flush(store.update(store.load(), s.reserve_event(store.load(), e)))
    now = NOW+timedelta(days=2)
    with pytest.raises(ValueError): s.settle(state, e, [1, 1], official_receipts(now), now)
    bad = official_receipts(now); bad[0]["content_hash"] = "corrupt"
    with pytest.raises(ValueError): s.settle(state, e, [0, 1], bad, now)
    state = store.flush(store.update(state, s.settle(state, e, [0, 1], official_receipts(now), now)))
    summary = s.summary(state)
    assert summary["scores"]["market"]["brier"] == .5 and summary["realized_profit"] is None
    other = copy.deepcopy(state["events"][e["id"]]); other["venue"] = "kalshi"
    state["events"]["kalshi:KPHL:2026-10-23"] = other
    assert s.summary(state)["settled_station_dates"] == 1
    assert s.summary(state)["scores"] == summary["scores"]


class Session:
    def __init__(self, data=None, error=None): self.data, self.error, self.calls = data, error, 0
    def get(self, *a, **kw):
        self.calls += 1
        if self.error: raise self.error
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.data)


def test_download_reservation_precedes_network_and_survives_failure(store, model):
    session = Session(error=requests.ConnectionError("fake"))
    f = collector.Forecasts(store, lambda: NOW, session=session, sleep=lambda _: None)
    params = receipt(model)["params"]
    with pytest.raises(requests.ConnectionError): f.fetch(ARCHIVE, params)
    assert len(store.load()["reservations"]) == 1
    assert runs.timestamp(store.load()["provider_retry_after"]) == NOW+timedelta(hours=24)
    restarted = collector.Forecasts(store, lambda: NOW+timedelta(hours=23), session=session, sleep=lambda _: None)
    with pytest.raises(ValueError, match="COOLDOWN"): restarted.fetch(ARCHIVE, params)
    assert session.calls == 1
    assert store.initialize(NOW)["provider_retry_after"] is not None


def test_success_cache_shared_without_redownload_and_corruption_not_erased(store, model):
    r = receipt(model)
    session = Session(data=r["data"])
    f = collector.Forecasts(store, lambda: NOW, session=session, sleep=lambda _: None)
    saved = f.fetch(ARCHIVE, r["params"])
    assert f.fetch(ARCHIVE, r["params"]) == saved
    assert session.calls == 1 and len(store.load()["reservations"]) == 1
    assert store.load()["provider_retry_after"] is None
    key = store.key("receipt", digest({"url": ARCHIVE, "params": r["params"]}))
    store.db.values[key]["receipt_id"] = "bad"
    with pytest.raises(ValueError): f.fetch(ARCHIVE, r["params"])
    assert session.calls == 1 and key in store.db.values


def test_rolling_budget_not_reset_at_midnight(store):
    state = store.load()
    state["reservations"] = [{"at": (NOW-timedelta(hours=14)).isoformat(), "request_id": str(i)} for i in range(150)]
    state = paper.signed(state, "state_hash")
    with pytest.raises(ValueError, match="BUDGET"): s.reserve_download(state, "new", NOW)
    after = s.reserve_download(state, "new", NOW+timedelta(hours=11))
    assert len(after["reservations"]) == 151  # Old ledger is retained, never cleared.


def test_outside_window_and_expired_cohort_never_download(store):
    class NoNetwork:
        def fetch(self, *a, **kw): raise AssertionError("network forbidden")
    for now in (NOW.replace(hour=5), NOW+timedelta(days=20)):
        report = collector.run(store, NoNetwork(), clock=lambda: now, markets=[])
        assert report["captured"] == 0 and report["forecast_downloads"] == 0
        assert report["paper_execution_enabled"] is False


def test_parent_identity_cannot_be_replaced(model):
    model["parameters"]["KPHL"]["sigma_f"] += .1
    model = paper.signed(model, "model_id")
    with pytest.raises(ValueError, match="PARENT"): s.verify_parent(model)


def test_collector_persists_once_then_grades_official_outcomes(store, model, monkeypatch):
    cs, qs, _ = inputs(model)
    markets = [{"_platform": "polymarket", "index": i} for i in range(2)]
    monkeypatch.setattr(collector, "resolve", lambda q, venue: cs[q["index"]])
    monkeypatch.setattr(collector, "refresh_quotes", lambda raw, venue, feeds: [{**q, "index": i} for i,q in enumerate(qs)])
    class FakeForecasts:
        downloads = 0
        metadata = {"gfs_global": metadata()}
        def fetch(self, *args): return receipt(model, RUN-timedelta(hours=6), high=75.)
        def pair(self, name, location):
            if name != "gfs_global": raise ValueError("RUN_INCOMPLETE_STANDARD_DAY")
            return receipt(model), self.fetch()
    feeds = FakeForecasts()
    result = collector.run(store, None, clock=lambda: NOW, markets=markets, forecasts=feeds)
    assert result["captured"] == 1 and result["decision_vectors"] == 1
    e = store.get("decision", "polymarket:KPHL:2026-10-23")
    assert e["auxiliary_rejections"] == {"gfs_hrrr": "RUN_INCOMPLETE_STANDARD_DAY", "ecmwf_ifs025": "RUN_INCOMPLETE_STANDARD_DAY"}
    assert collector.run(store, None, clock=lambda: NOW, markets=markets, forecasts=feeds)["captured"] == 0
    now = NOW+timedelta(days=2)
    monkeypatch.setattr(collector, "official_outcomes", lambda *args: ([0, 1], official_receipts(now)))
    graded = collector.run(store, None, clock=lambda: now, markets=[], forecasts=feeds)
    assert graded["graded"] == 1 and graded["settled_station_dates"] == 1
    assert graded["scores"]["guard_market_fallback"] == graded["scores"]["market"]
    assert store.db.values["weather_daytime_paper_v1:book"] == {"must": "remain untouched"}


def test_failed_request_cooldown_starts_after_error_and_no_unreserved_network(store, model):
    clock = [NOW]
    class LateFailure:
        def get(self, *args, **kwargs):
            state = store.load()
            assert len(state["reservations"]) == 1 and state["provider_retry_after"] is not None
            clock[0] += timedelta(seconds=30)
            raise requests.Timeout()
    f = collector.Forecasts(store, lambda: clock[0], session=LateFailure(), sleep=lambda _: None)
    with pytest.raises(requests.Timeout): f.fetch(ARCHIVE, receipt(model)["params"])
    assert runs.timestamp(store.load()["provider_retry_after"]) == clock[0]+timedelta(hours=24)


def test_unknown_write_or_killed_worker_never_reuses_reservation(store, model):
    before = store.load()
    store.update(before, s.reserve_download(before, "interrupted", NOW))
    session = Session(data=receipt(model)["data"])
    f = collector.Forecasts(store, lambda: NOW+timedelta(minutes=1), session=session, sleep=lambda _: None)
    with pytest.raises(ValueError, match="COOLDOWN"): f.fetch(ARCHIVE, receipt(model)["params"])
    assert session.calls == 0 and len(store.load()["reservations"]) == 1


def test_a_new_state_cannot_adopt_past_decisions(store, model):
    state = copy.deepcopy(store.load())
    state["created_at"] = (NOW+timedelta(seconds=1)).isoformat()
    state = paper.signed(state, "state_hash")
    with pytest.raises(ValueError, match="EVENT_SCOPE"): s.reserve_event(state, event(model))


def test_comparison_uncertainty_uses_dates_not_quote_or_venue_counts(store):
    state = store.load()
    for day in range(7):
        for venue in ("kalshi", "polymarket"):
            score = .2 if day % 2 else .8
            state["events"][f"{venue}:{day}"] = {"station": "KPHL", "target": str(day), "status": "settled",
                "scores": {a: {"brier": .5 if a == "market" else score, "log_loss": 1.} for a in s.ARMS}}
    report = s.summary(state)
    assert report["settled_station_dates"] == 7 and report["decision_vectors"] == 14
    ci = report["paired_comparisons"]["fresh_gfs_ablation"]["date_block_bootstrap_95_interval"]
    assert ci[0] < 0 < ci[1]
    for key in [k for k in state["events"] if k.startswith("polymarket")]: del state["events"][key]
    assert s.summary(state)["paired_comparisons"] == report["paired_comparisons"]
    del state["events"]["kalshi:6"]
    assert s.summary(state)["paired_comparisons"]["fresh_gfs_ablation"]["date_block_bootstrap_95_interval"] is None


def test_saved_live_probe_receipts_revalidate_without_network(model):
    root = Path(__file__).resolve().parents[2]/"research/weather_verified_shadow/probe_20261023"
    raw = [json.loads(p.read_text()) for p in root.glob('*.json')]
    receipts = {digest(r): r for r in raw if "url" in r}
    original = json.loads((root/'summary.json').read_text())
    primary = json.loads((root/'primary_gfs_summary.json').read_text())
    for name in runs.MODEL_DOMAINS:
        saved = primary['pair'] if name == 'gfs_global' else original['models'][name]
        now = runs.timestamp(primary['completed_at'] if name == 'gfs_global' else original['completed_at'])
        new, old = (receipts[saved[k]['receipt_id']] for k in ('current','previous'))
        meta = next(r for r in receipts.values() if r['url'] == runs.metadata_url(name))
        pair = runs.verified_pair(new, old, model=name, runs=runs.select_pair(name, meta, now), station='KPHL',
            target=saved['current']['target_date'], location=model['locations']['KPHL'], decision_at=now)
        assert pair['high_revision_f'] == pytest.approx(saved['high_revision_f'])
        assert pair['current']['exact_publication_time_verified'] is False
