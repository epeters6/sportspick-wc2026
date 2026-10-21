import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.ml.weather_learning.dataset import digest
from backend.weather_backfill_checkpoint import OWNER_FILE, PLAN_ID, pack, sha256, unpack, validate_owner, validate_state
from backend.weather_decision_archive import make_plan
from scripts.run_weather_hosted_backfill import budget_available, latest_generation


def state(tmp_path):
    root = tmp_path/'source'
    for name in ('archive','cache','retest'): (root/name).mkdir(parents=True)
    plan = make_plan(date(2026,4,3), 200, [8,11,14,17], 1)
    assert plan['plan_id'] == PLAN_ID
    data = []
    dataset_hash = digest(data)
    latest = {'plan_id':PLAN_ID, 'dataset_hash':dataset_hash, 'dataset_file':dataset_hash+'_dataset.json'}
    values = {'archive/plan.json':plan, 'archive/latest.json':latest, 'archive/'+latest['dataset_file']:data,
              'retest/backfill_progress.json':{**latest, 'state':'waiting_for_next_budgeted_batch'},
              'cache/decision_archive_usage.json':{'version':1, 'days':{}},
              'cache/full_backfill_rolling_usage.json':{'version':1, 'reservations':[]},
              'cache/'+OWNER_FILE:{'version':1,'plan_id':PLAN_ID,'executor':'github_actions'},
              'market.json':{'v3':[]}}
    for name, value in values.items(): (root/name).write_text(json.dumps(value), encoding='utf-8')
    return root


def test_round_trip_preserves_every_evidence_byte_and_owner(tmp_path):
    src=state(tmp_path)
    bundle=tmp_path/'checkpoint.zip'
    result=pack(src/'archive',src/'cache',src/'market.json',src/'retest',bundle)
    target=tmp_path/'restored'
    unpack(bundle,result['sha256'],target)
    for path in src.rglob('*'):
        if path.is_file(): assert path.read_bytes()==(target/path.relative_to(src)).read_bytes()
    with pytest.raises(ValueError, match='OWNER_MISMATCH'): validate_owner(target/'cache','local')


def test_missing_ledger_and_active_worker_cannot_be_checkpointed(tmp_path):
    src=state(tmp_path)
    lock=src/'cache/weather_full_backfill_worker.lock'
    lock.write_text('active')
    with pytest.raises(ValueError, match='LOCK_PRESENT'): validate_state(src/'archive',src/'cache',src/'retest')
    # Test fixture cleanup only, after explicitly creating and owning this lock.
    lock.unlink()
    (src/'cache/decision_archive_usage.json').rename(src/'cache/saved-ledger.json')
    with pytest.raises(FileNotFoundError): validate_state(src/'archive',src/'cache',src/'retest')


def test_tampered_and_traversal_bundles_are_rejected(tmp_path):
    src=state(tmp_path)
    bundle=tmp_path/'checkpoint.zip'
    result=pack(src/'archive',src/'cache',src/'market.json',src/'retest',bundle)
    with pytest.raises(ValueError,match='HASH_MISMATCH'):unpack(bundle,'0'*64,tmp_path/'bad')
    malicious=tmp_path/'malicious.zip'
    with zipfile.ZipFile(bundle) as old, zipfile.ZipFile(malicious,'w') as new:
        manifest=json.loads(old.read('manifest.json'))
        manifest['files']['../outside.json']={'size':2,'sha256':digest({})}
        for name in old.namelist():
            new.writestr(name,json.dumps(manifest) if name=='manifest.json' else old.read(name))
        new.writestr('../outside.json','{}')
    with pytest.raises(ValueError,match='UNSAFE_CHECKPOINT_MEMBER'):unpack(malicious,sha256(malicious),tmp_path/'bad2')
    assert not (tmp_path/'outside.json').exists()


def test_latest_incomplete_generation_never_rolls_back_budget():
    asset=lambda name:{'name':name,'id':1,'size':1}
    seed=[asset('000000-checkpoint.json'),asset('000000-checkpoint.zip')]
    assert latest_generation(seed)[0]==0
    with pytest.raises(ValueError,match='INCOMPLETE_HOSTED'):
        latest_generation(seed+[asset('000001-lease.json')])
    complete=seed+[asset('000001-'+suffix) for suffix in ('lease.json','checkpoint.zip','checkpoint.json')]
    assert latest_generation(complete)[0]==1


def test_hosted_requires_handoff_and_budgets_and_cooldown_are_preserved(tmp_path):
    validate_owner(tmp_path,'local')
    with pytest.raises(ValueError,match='EXPLICIT_HANDOFF'):validate_owner(tmp_path,'github_actions')
    src=state(tmp_path)
    now=datetime.now(timezone.utc)
    assert budget_available(src,now)
    (src/'cache/full_backfill_rolling_usage.json').write_text(json.dumps({'version':1,'reservations':[{'at':now.isoformat(),'units':3000}]}))
    assert not budget_available(src,now)
    (src/'cache/full_backfill_rolling_usage.json').write_text(json.dumps({'version':1,'reservations':[]}))
    p=src/'retest/backfill_progress.json'
    v=json.loads(p.read_text());v['retry_not_before']=(now+timedelta(hours=23)).isoformat();p.write_text(json.dumps(v))
    assert not budget_available(src,now)


@pytest.mark.parametrize('lose_checkpoint', [False, True])
def test_hosted_commit_order_and_interruption_never_reuses_old_ledger(tmp_path, monkeypatch, lose_checkpoint):
    from scripts import run_weather_hosted_backfill as hosted
    src = state(tmp_path)
    seed = tmp_path/'seed.zip'
    packed = pack(src/'archive', src/'cache', src/'market.json', src/'retest', seed)
    blobs = {
        '000000-checkpoint.zip': seed.read_bytes(),
        '000000-checkpoint.json': json.dumps({'version':1, 'generation':0, 'plan_id':PLAN_ID,
                                             'bundle_sha256':packed['sha256'], 'complete':False}).encode()}
    events = []
    def listing(repo):
        return [{'name':name, 'id':index, 'size':len(data)} for index,(name,data) in enumerate(blobs.items())]
    def download(repo, asset, path):
        path.write_bytes(blobs[asset['name']])
        events.append('download:'+asset['name'])
    def upload(repo, path):
        events.append('upload:'+path.name)
        if lose_checkpoint and path.name.endswith('checkpoint.zip'):
            raise RuntimeError('simulated upload loss')
        assert path.name not in blobs
        blobs[path.name] = path.read_bytes()
    def batch(*args):
        assert '000001-lease.json' in blobs
        events.append('provider_batch')
        return {'state':'waiting_for_next_budgeted_batch', 'location_units_this_batch':0}
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    # Track this env before main changes it, so it cannot affect local tests.
    monkeypatch.setenv('WEATHER_BACKFILL_EXECUTOR', 'local')
    monkeypatch.setattr('sys.argv', ['hosted', '--repo', 'owner/repo'])
    monkeypatch.setattr(hosted, 'assets', listing)
    monkeypatch.setattr(hosted, 'download', download)
    monkeypatch.setattr(hosted, 'upload', upload)
    monkeypatch.setattr(hosted, 'run_batch', batch)
    if lose_checkpoint:
        with pytest.raises(RuntimeError, match='simulated'): hosted.main()
        with pytest.raises(ValueError, match='INCOMPLETE_HOSTED'): hosted.main()
        assert events.count('provider_batch') == 1
        assert '000001-checkpoint.json' not in blobs
    else:
        assert hosted.main() == 0
        lease, call, bundle, check, commit = (events.index(x) for x in (
            'upload:000001-lease.json', 'provider_batch', 'upload:000001-checkpoint.zip',
            'download:000001-checkpoint.zip', 'upload:000001-checkpoint.json'))
        assert lease < call < bundle < check < commit
        assert json.loads(blobs['000001-checkpoint.json'])['parent_bundle_sha256'] == packed['sha256']


def test_local_batch_refuses_hosted_owner_before_any_collection(tmp_path, monkeypatch):
    from scripts import run_weather_full_backfill as worker
    src = state(tmp_path)
    monkeypatch.setenv('WEATHER_BACKFILL_EXECUTOR', 'local')
    def forbidden(*args, **kwargs):
        pytest.fail('collection must not run after cloud handoff')
    monkeypatch.setattr(worker, 'collect', forbidden)
    with pytest.raises(ValueError, match='OWNER_MISMATCH'):
        worker.run_batch(src/'archive',src/'cache',src/'market.json',src/'retest')
    assert not list((src/'cache').glob('*.lock'))
