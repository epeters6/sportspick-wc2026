import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import weather_collection_runtime as runtime


def test_outside_all_station_windows_performs_no_discovery(tmp_path, monkeypatch):
    clock = lambda: datetime(2026, 10, 22, 2, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, 'discover_catalog', Mock(side_effect=AssertionError('network')))
    collector = Mock(return_value={'errors':[], 'snapshots':0, 'status':'healthy'})
    result = runtime.collect_current(tmp_path, collector=collector, clock=clock)
    assert collector.call_args.kwargs['markets'] == []
    assert result['acquisition']['outside_all_station_windows']
    assert collector.call_args.kwargs['clock']() == clock()


def test_duplicate_preflight_is_read_only_and_skips_discovery(tmp_path):
    db = Mock()
    db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{'value':{'run_id':'owner'}}]
    collect = Mock(side_effect=AssertionError('must not collect'))
    result = runtime.collect_current(tmp_path, collector=collect, db=db)
    assert result['reason'] == 'HOURLY_SLOT_ALREADY_ATTEMPTED'
    assert not db.table.return_value.upsert.called
    assert not collect.called


@pytest.mark.parametrize('existing', [False, True])
def test_duplicate_skip_never_reads_snapshot_file(tmp_path, existing):
    if existing:
        (tmp_path/'snapshots.jsonl').write_text('invalid stale evidence')
    assert runtime.guard_inputs(tmp_path, {'status':'skipped', 'reason':'HOURLY_SLOT_ALREADY_ATTEMPTED',
                                        'snapshots':0, 'errors':[]}, datetime.now(timezone.utc)) is None
    if existing:
        assert (tmp_path/'snapshots.jsonl').read_text() == 'invalid stale evidence'


def test_genuine_missing_stale_or_partial_artifact_still_fails(tmp_path):
    now = datetime.now(timezone.utc)
    with pytest.raises(FileNotFoundError):
        runtime.guard_inputs(tmp_path, {'snapshots':1}, now)
    (tmp_path/'snapshots.jsonl').write_text('')
    with pytest.raises(ValueError, match='COUNT_MISMATCH'):
        runtime.guard_inputs(tmp_path, {'snapshots':1}, now)
    (tmp_path/'snapshots.jsonl').write_text(json.dumps({'metadata':{'decision_at':'2020-01-01T00:00:00Z'}}))
    with pytest.raises(ValueError, match='STALE'):
        runtime.guard_inputs(tmp_path, {'snapshots':1}, now)


def test_budget_expiry_stops_before_another_request(tmp_path, monkeypatch):
    feeds = runtime.CollectionFeeds(tmp_path)
    feeds.deadline = 10
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: 11)
    monkeypatch.setattr(runtime.PublicFeeds, 'fetch', Mock(side_effect=AssertionError('network')))
    with pytest.raises(ValueError, match='TIME_BUDGET'):
        feeds.fetch('https://example.invalid', {})


def test_corrupt_receipt_is_preserved_without_refresh(tmp_path, monkeypatch):
    url, params = 'https://example.invalid', {'item':1}
    path = tmp_path/(runtime.digest({'url':url,'params':params})+'.json')
    raw = json.dumps({'url':url,'params':params,'data':{'corrupt':True},'content_hash':'wrong',
                      'received_at':'2026-01-01T00:00:00Z'})
    path.write_text(raw)
    monkeypatch.setattr(runtime.PublicFeeds,'fetch',Mock(side_effect=AssertionError('network')))
    with pytest.raises(ValueError,match='CORRUPT_PUBLIC_FEED_RECEIPT'):
        runtime.CollectionFeeds(tmp_path).fetch(url,params)
    assert path.read_text() == raw


def test_guard_file_must_belong_to_current_collection_run(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path/'snapshots.jsonl').write_text(json.dumps({'metadata':{'run_id':'other','decision_at':now.isoformat()}}))
    with pytest.raises(ValueError,match='RUN_MISMATCH'):
        runtime.guard_inputs(tmp_path, {'run_id':'expected','snapshots':1}, now)


def test_poly_catalog_uses_only_catalog_requests_and_preserves_rules():
    from backend.ml.weather_learning.contracts import resolve
    description = ('This market will resolve to Yes if the highest temperature recorded at Chicago Midway International '
                   '(CLIMDW) on October 23, 2026 is less than or equal to 69°F, according to the NWS Daily Climate Report.')
    raw = {'slug':'chicago-high-69', 'outcome':'69 or below', 'description':description,
           'resolutionSource':'NWS CLI', 'settlement_station':'KMDW'}
    event = {'slug':'chicago-oct23', 'title':'Highest temperature in Chicago on October 23, 2026?',
             'endTime':'2026-10-24T06:00:00Z', 'markets':[raw]}
    calls = []
    def fetch(url, params, **kwargs):
        calls.append(url)
        assert params['categories'] == ['climate']
        resource = url.rsplit('/',1)[-1]
        data = {resource: [] if resource=='markets' else [event]}
        return {'data':data, 'received_at':'2026-10-22T13:00:00Z', 'content_hash':runtime.digest(data)}
    rows, receipts = runtime.poly_catalog(SimpleNamespace(fetch=fetch))
    assert len(rows) == 1 and len(calls) == 2
    assert not any('book' in u or 'bbo' in u for u in calls)
    assert rows[0]['description'] == description
    assert not any(k in rows[0] for k in ('yes_bid','yes_ask','best_bid','best_ask'))
    contract = resolve(rows[0], 'polymarket')
    assert contract.station == 'KMDW' and contract.target_date == '2026-10-23'
    assert contract.high_f == 69.5


def test_truncated_catalog_fails_closed():
    feed = Mock()
    feed.fetch.side_effect = lambda u,p,**kw: {'data':{'markets':[{'slug':str(p['offset']+i)} for i in range(400)]},
                                              'received_at':'2026-10-22T13:00:00Z', 'content_hash':'hash'}
    with pytest.raises(ValueError, match='PAGINATION_LIMIT'):
        runtime.pages(feed, 'markets', 400)
    assert feed.fetch.call_count == 11


def test_rate_limit_stops_same_host_without_blocking_other_venue(tmp_path, monkeypatch):
    feeds = runtime.CollectionFeeds(tmp_path)
    fetch = Mock(side_effect=[RuntimeError('PUBLIC_FEED_RATE_LIMIT'), {'data':{}}])
    monkeypatch.setattr(runtime.PublicFeeds, 'fetch', fetch)
    with pytest.raises(RuntimeError, match='RATE_LIMIT'):
        feeds.fetch('https://gateway.polymarket.us/v1/events', {})
    with pytest.raises(ValueError, match='HOST_RATE_LIMITED'):
        feeds.fetch('https://gateway.polymarket.us/v1/markets/M/book', {})
    assert feeds.fetch('https://external-api.kalshi.com/markets', {}) == {'data':{}}
    assert fetch.call_count == 2


def test_repeated_catalog_page_cannot_masquerade_as_complete():
    feed = Mock()
    feed.fetch.return_value = {'data':{'markets':[{'slug':str(i)} for i in range(400)]},
                              'received_at':'2026-10-22T13:00:00Z', 'content_hash':'hash'}
    with pytest.raises(ValueError, match='DUPLICATE_OR_MISSING'):
        runtime.pages(feed, 'markets', 400)
    assert feed.fetch.call_count == 2


def test_discovery_errors_remain_visible_after_successful_partial_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'eligible_hours', lambda _: True)
    errors = [{'venue':'polymarket','reason':'PUBLIC_FEED_RATE_LIMIT'}]
    monkeypatch.setattr(runtime, 'discover_catalog', lambda _: ([{'ticker':'valid'}], errors, []))
    result = runtime.collect_current(tmp_path, collector=lambda *a, **k: {'snapshots':12,'errors':[],'status':'healthy'})
    assert result['status'] == 'degraded' and result['errors'] == errors and result['snapshots'] == 12


def test_missing_catalog_rows_are_unknown_not_empty_success(monkeypatch):
    monkeypatch.setattr(runtime, 'poly_catalog', lambda _: ([{'ticker':'poly'}], []))
    feeds = Mock()
    feeds.fetch.return_value = {'data':{}, 'received_at':'2026-10-22T13:00:00Z', 'content_hash':'hash'}
    markets, errors, _ = runtime.discover_catalog(feeds)
    assert markets == [{'ticker':'poly'}]
    assert errors and all(e['venue']=='kalshi' and e['reason']=='MALFORMED_KALSHI_CATALOG' for e in errors)


def test_acquisition_wrapper_keeps_frozen_forecast_rows_byte_identical(tmp_path, monkeypatch):
    import uuid
    from backend.tests.test_weather_learning import NowcastTests, market
    from backend.ml.weather_learning.collection import collect
    _, forecast, obs, decision = NowcastTests().bundle(hour=10)
    forecast['content_hash'], obs['content_hash'] = 'forecast-hash', 'observation-hash'
    low = market(yes_bid=1, yes_ask=2, received_timestamp=decision.isoformat(), _platform='kalshi')
    high = market(ticker='HIGH', strike_type='greater', floor_strike=74, cap_strike=None,
                  rules_primary=market()['rules_primary'].replace('less than 75','greater than 74'),
                  yes_bid=97, yes_ask=98, received_timestamp=decision.isoformat(), _platform='kalshi')
    feeds = Mock()
    feeds.observations.return_value, feeds.forecast.return_value = obs, forecast
    feeds.station.return_value = {'station':'KPHL','lat':39.87,'lon':-75.24,'elevation_m':11}
    monkeypatch.setattr('backend.ml.weather_learning.collection.refresh_quotes', lambda *a: [low,high])
    monkeypatch.setattr('backend.ml.weather_learning.collection.uuid.uuid4', lambda: uuid.UUID(int=1))
    monkeypatch.setattr(runtime, 'discover_catalog', lambda _: ([low,high],[],[]))
    direct, wrapped = tmp_path/'direct', tmp_path/'wrapped'
    before = collect(direct, feeds=feeds, markets=[low,high], clock=lambda:decision)
    after = runtime.collect_current(wrapped, collector=collect, feeds=feeds, clock=lambda:decision)
    assert before['snapshots'] == after['snapshots'] == 2
    assert (direct/'snapshots.jsonl').read_bytes() == (wrapped/'snapshots.jsonl').read_bytes()
