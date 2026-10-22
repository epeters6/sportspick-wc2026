import copy
import json
from datetime import datetime, timedelta, timezone, date
from types import SimpleNamespace

import pytest

from backend import weather_daytime_paper as p
from backend.ml.weather_learning.contracts import Contract
from backend.ml.weather_learning.dataset import digest, utc
from backend.tests.test_weather_station_distribution import history
from backend.weather_historical_calibration import ARCHIVE, standard_day

NOW = datetime(2026, 10, 22, 12, 5, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    root = tmp_path_factory.mktemp("daytime-fit")
    rows = history("2026-04-03", 199, tuple(p.SPEC["stations"]))
    (root / "dataset.json").write_text(json.dumps(rows))
    status = {"status": "complete_with_gaps", "dataset_hash": digest(rows),
              "stations": {s: {"station_coordinates": {"lat": 40., "lon": -75., "elev": 10.}}
                           for s in p.SPEC["stations"]}}
    (root / "latest.json").write_text(json.dumps(status))
    return p.fit(root, clock=lambda: NOW - timedelta(days=1)), root


def inputs(model):
    target, station = "2026-10-23", "KPHL"
    start, end = standard_day(station, date.fromisoformat(target))
    common = dict(platform="polymarket", station=station, target_date=target, metric="high", authority="NWS CLI",
                  observation_timezone="Etc/GMT+5", window_start=start.isoformat(), window_end=end.isoformat(),
                  rules_text="synthetic", rules_hash="rules")
    contracts = [Contract(market_id="a", low_f=None, high_f=75.5, **common),
                 Contract(market_id="b", low_f=75.5, high_f=None, **common)]
    quotes = [{"ticker": name, "best_bid": .1, "best_ask": .2, "ask_size": 1,
               "execution_price_source": "orderbook", "received_timestamp": (NOW-timedelta(seconds=1)).isoformat()}
              for name in ("a", "b")]
    data = {"latitude": 40., "longitude": -75., "elevation": 10., "utc_offset_seconds": 0,
            "hourly_units": {"temperature_2m": "°F"},
            "hourly": {"time": [int(start.timestamp())+i*3600 for i in range(24)], "temperature_2m": [75.]*24}}
    receipt = {"url": ARCHIVE, "params": {"run": "2026-10-22T00:00", "models": "gfs_global",
               "hourly": "temperature_2m", "timezone": "GMT", "latitude": 40., "longitude": -75., "elevation": 10.},
               "received_at": (NOW-timedelta(minutes=1)).isoformat(), "data": data, "content_hash": digest(data)}
    return contracts, quotes, receipt


def event(model):
    return p.decision(model, *inputs(model), NOW)


def official_receipts(now):
    result = []
    for market_id, y in (("a", 0), ("b", 1)):
        data = {"slug": market_id, "settlement": y}
        result.append({"url": "https://gateway.polymarket.us/v1/markets/"+market_id+"/settlement",
                       "data": data, "content_hash": digest(data), "received_at": now.isoformat()})
    return result


def test_fit_frozen_before_forward_cohort_and_same_parent_recipe(model):
    m, root = model
    p.verify_model(m)
    assert m["training_last_target"] == "2026-10-18"
    assert len(m["parameters"]) == 21
    assert m["historical_as_of_verified"] is False
    assert m["archive_production_training_eligible"] is False
    rows = json.loads((root/"dataset.json").read_text())
    assert all(r["as_of_eligible"] is False for r in rows)
    with pytest.raises(ValueError, match="PRECEDE_COHORT"):
        p.fit(root, clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    bad = copy.deepcopy(m); bad["parameters"]["KPHL"]["bias_f"] += 1
    with pytest.raises(ValueError, match="IDENTITY"):
        p.verify_model(bad)


def test_first_future_capture_probability_and_cost_parity(model):
    m, _ = model
    e = event(m)
    assert sum(e["probabilities"]) == pytest.approx(1)
    assert e["mean_f"] == pytest.approx(75 + m["parameters"]["KPHL"]["bias_f"] + m["parameters"]["KPHL"]["residual_shift_f"])
    assert e["costs"] == pytest.approx([.215, .215])
    assert e["market_probabilities"] == [.5, .5]
    assert e["model_created_at"] < e["decision_at"]


@pytest.mark.parametrize("change", ["wrong_run", "wrong_model", "wrong_grid", "wrong_units", "missing_hour", "future_weather", "future_quote", "stale_quote", "nighttime", "missing_partition", "future_model", "outside_window", "wrong_target"])
def test_forward_timing_and_input_scope_fail_closed(model, change):
    m = copy.deepcopy(model[0]); cs, qs, r = inputs(m); now = NOW
    if change == "wrong_run": r["params"]["run"] = "2026-10-22T06:00"
    if change == "wrong_model": r["params"]["models"] = "best_match"
    if change == "wrong_grid": r["data"]["latitude"] = 30
    if change == "wrong_units": r["data"]["hourly_units"]["temperature_2m"] = "°C"
    if change == "missing_hour": r["data"]["hourly"]["temperature_2m"][0] = None
    if change == "future_weather": r["received_at"] = (NOW+timedelta(seconds=1)).isoformat()
    if change == "future_quote": qs[0]["received_timestamp"] = (NOW+timedelta(seconds=1)).isoformat()
    if change == "stale_quote": qs[0]["received_timestamp"] = (NOW-timedelta(minutes=10)).isoformat()
    if change == "nighttime": cs[0] = Contract(**{**cs[0].record(), "metric": "low"})
    if change == "missing_partition": cs[0] = Contract(**{**cs[0].record(), "high_f": 74.5})
    if change == "future_model": m = p.signed({**m, "created_at": (NOW+timedelta(minutes=1)).isoformat()}, "model_id")
    if change == "outside_window": now += timedelta(hours=1)
    if change == "wrong_target": cs = [Contract(**{**c.record(), "target_date": "2026-10-24"}) for c in cs]
    r["content_hash"] = digest(r["data"])
    with pytest.raises(ValueError):
        p.decision(m, cs, qs, r, now)


def test_one_contract_depth_fee_and_duplicate_guards(model):
    m, _ = model
    b = p.initial_book(m)
    e = event(m)
    updated = p.reserve(b, e)
    assert updated["pending"]["record"]["position"]["shares"] == 1
    assert p.account(updated, "polymarket", "2026-10-22")["reserved"] == pytest.approx(.215)
    with pytest.raises(ValueError, match="DUPLICATE"):
        p.reserve(updated, e)
    e["depths"] = [.99, 0]
    assert p.reserve(b, e)["pending"]["record"]["position"] is None
    e["depths"] = [1, 1]; e["costs"] = [None, None]
    assert p.reserve(b, e)["pending"]["record"]["position"] is None


def test_open_daily_and_cumulative_stops_do_not_reset(model):
    m, _ = model; e = event(m)
    for fields in ({"status": "pending", "cost": 9.9},
                   {"status": "settled", "cost": 10, "pnl": -10, "settled_at": NOW.isoformat()},
                   {"status": "settled", "cost": 50, "pnl": -50, "settled_at": "2026-10-21T00:00:00Z"}):
        b = p.initial_book(m)
        b["events"]["older"] = {"venue": "polymarket", **fields}
        b = p.signed(b, "state_hash")
        assert p.reserve(b, e)["pending"]["record"]["position"] is None
    b = p.initial_book(m); b["halted_venues"] = ["polymarket"]
    assert p.reserve(p.signed(b, "state_hash"), e)["pending"]["record"]["position"] is None


def test_official_settlement_and_matched_market_scores(model):
    b = p.reserve(p.initial_book(model[0]), event(model[0]))
    e = b["pending"]["record"]; b["pending"] = None; b = p.signed(b, "state_hash")
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="OUTCOME_VECTOR"):
        p.settle(b, e, [1, 1], [], now)
    with pytest.raises(ValueError, match="OUTCOME_VECTOR"):
        p.settle(b, e, [0, 1], [], NOW)
    with pytest.raises(ValueError, match="RECEIPTS_MISSING"):
        p.settle(b, e, [0, 1], [], now)
    bad = official_receipts(now); bad[1]["data"]["slug"] = "other"
    with pytest.raises(ValueError, match="RECEIPT_MISMATCH"):
        p.settle(b, e, [0, 1], bad, now)
    out = p.settle(b, e, [0, 1], official_receipts(now), now)
    assert p.account(out, "polymarket", "2026-10-24")["realized_paper_pnl"] == pytest.approx(.785)
    assert p.summary(out)["scores"]["market"]["brier"] == .5
    duplicate_venue = copy.deepcopy(out["events"][e["id"]]); duplicate_venue["venue"] = "kalshi"
    out["events"]["kalshi:KPHL:2026-10-23"] = duplicate_venue
    assert p.summary(out)["settled_station_dates"] == 1


class MemoryDB:
    def __init__(self): self.values = {}; self.fail_write = False
    def table(self, name):
        assert name == "app_settings"
        self.mode = "read"; self.filters = []; return self
    def select(self, fields): return self
    def eq(self, k, v): self.filters.append((k, v)); return self
    def upsert(self, row, **kw): self.mode = "insert"; self.row = row; self.ignore = kw.get("ignore_duplicates", False); return self
    def update(self, row): self.mode = "update"; self.row = row; return self
    def execute(self):
        if self.mode == "insert":
            if self.fail_write: raise RuntimeError("injected lost write")
            key = self.row["key"]
            if key not in self.values or not self.ignore: self.values[key] = copy.deepcopy(self.row["value"])
            return SimpleNamespace(data=[])
        result = []
        for k,v in list(self.values.items()):
            if all((k if f == "key" else v["state_hash"]) == expected for f,expected in self.filters):
                if self.mode == "update": self.values[k] = v = copy.deepcopy(self.row["value"])
                result.append({"key": k, "value": copy.deepcopy(v)})
        return SimpleNamespace(data=result)


def test_journal_recovers_crash_and_cas_blocks_stale_writer(model, tmp_path):
    m, _ = model; db = MemoryDB(); store = p.Store(db, m, tmp_path)
    with pytest.raises(ValueError, match="MISSING"):
        store.load()
    db.values[store.key("book")] = p.initial_book(m)
    before = store.load(); after = store.update(before, p.reserve(before, event(m)))
    db.fail_write = True
    with pytest.raises(RuntimeError): store.flush(after)
    assert store.load()["pending"] is not None
    db.fail_write = False
    done = store.flush(store.load())
    assert done["pending"] is None and len(done["events"]) == 1
    assert store.flush(done) == done
    with pytest.raises(ValueError, match="CONCURRENT"):
        store.update(before, after)
    saved = store.get("decision", event(m)["id"])
    saved["mean_f"] += 1
    with pytest.raises(ValueError, match="CONFLICT"):
        store.immutable("decision", event(m)["id"], saved)


def test_collection_persists_once_grades_later_and_respects_no_backfill(model, tmp_path, monkeypatch):
    from backend import weather_daytime_collection as collector
    m, _ = model; db = MemoryDB(); store = p.Store(db, m, tmp_path)
    db.values[store.key("book")] = p.initial_book(m)
    cs, qs, receipt = inputs(m)
    markets = [{"_platform": "polymarket", "index": i} for i in range(2)]
    monkeypatch.setattr(collector, "resolve", lambda q, venue: cs[q["index"]])
    monkeypatch.setattr(collector, "refresh_quotes", lambda raw, venue, feeds: [{**q, "index": i} for i,q in enumerate(qs)])
    cache = SimpleNamespace(get=lambda *args: receipt, downloaded=1)
    report = collector.run(store, None, clock=lambda: NOW, markets=markets, archive_cache=cache)
    assert report["captured"] == 1 and report["decision_vectors"] == 1
    assert store.get("decision", "polymarket:KPHL:2026-10-23")["model_id"] == m["model_id"]
    again = collector.run(store, None, clock=lambda: NOW+timedelta(minutes=1), markets=markets, archive_cache=cache)
    assert again["captured"] == 0
    monkeypatch.setattr(collector, "official_outcomes", lambda *args: ([0, 1], official_receipts(NOW+timedelta(days=2))))
    graded = collector.run(store, None, clock=lambda: NOW+timedelta(days=2), markets=[], archive_cache=cache)
    assert graded["graded"] == 1 and graded["settled_station_dates"] == 1
    assert len(db.values) == 4  # book, decision, settlement, latest


def test_provider_failure_has_durable_24h_cooldown(model, tmp_path, monkeypatch):
    from backend import weather_daytime_collection as collector
    m, _ = model; db = MemoryDB(); store = p.Store(db, m, tmp_path)
    db.values[store.key("book")] = p.initial_book(m)
    cs, _, _ = inputs(m)
    monkeypatch.setattr(collector, "resolve", lambda q, venue: cs[q["index"]])
    calls = []
    def failed(*args):
        calls.append(1)
        raise RuntimeError("provider rate limit")
    cache = SimpleNamespace(get=failed, downloaded=1)
    markets = [{"_platform": "polymarket", "index": i} for i in range(2)]
    for now in (NOW, NOW+timedelta(hours=3)):
        collector.run(store, None, clock=lambda: now, markets=markets, archive_cache=cache)
    assert len(calls) == 1
    assert utc(store.load()["provider_retry_after"]) == NOW+timedelta(hours=24)


def test_corrupt_public_receipt_is_preserved(model, tmp_path):
    from backend.weather_daytime_collection import StrictPublicFeeds
    path = tmp_path / (digest({"url": "https://example.invalid", "params": {}})+".json")
    saved = {"url": "https://example.invalid", "params": {}, "data": {}, "content_hash": "wrong", "received_at": NOW.isoformat()}
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="CORRUPT"):
        StrictPublicFeeds(tmp_path, clock=lambda: NOW).fetch("https://example.invalid", {})
    assert json.loads(path.read_text()) == saved
