"""One hosted batch from durable draft-release checkpoints; fail closed on gaps.

No database keys, trading credentials, paid services or cache-reset fallback.
A lease uploaded BEFORE download makes a lost runner/checkpoint visible instead
of allowing the next runner to reuse an old quota ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_backfill_checkpoint import PLAN_ID, pack, sha256, unpack
from scripts.run_weather_full_backfill import run_batch

TAG = 'weather-backfill-state-2026-04-03-2026-10-19'


def gh(*args, binary=False):
    result = subprocess.run(['gh', *args], capture_output=True, check=False)
    if result.returncode:
        # Never echo an API response, authenticated URL or environment.
        raise RuntimeError('GITHUB_CHECKPOINT_OPERATION_FAILED')
    return result.stdout if binary else json.loads(result.stdout)


def assets(repo):
    releases = gh('api', f'repos/{repo}/releases?per_page=100', '--paginate', '--slurp')
    matches = [r for page in releases for r in page if r['tag_name'] == TAG]
    if len(matches) != 1 or not matches[0].get('draft'):
        raise ValueError('CHECKPOINT_RELEASE_MUST_REMAIN_DRAFT')
    release = matches[0]
    pages = gh('api', f'repos/{repo}/releases/{release["id"]}/assets?per_page=100', '--paginate', '--slurp')
    return [asset for page in pages for asset in page]


def latest_generation(values):
    indexed = {}
    for value in values:
        match = re.fullmatch(r'(\d{6})-(lease\.json|checkpoint\.json|checkpoint\.zip)', value['name'])
        if match:
            key = (int(match[1]), match[2])
            if key in indexed:
                raise ValueError('DUPLICATE_HOSTED_CHECKPOINT_ASSET')
            indexed[key] = value
    if not indexed:
        raise ValueError('HOSTED_CHECKPOINT_SEED_REQUIRED')
    latest = max(n for n, _ in indexed)
    required = ('checkpoint.json', 'checkpoint.zip') + (('lease.json',) if latest else ())
    if any((latest, name) not in indexed for name in required):
        raise ValueError('INCOMPLETE_HOSTED_CHECKPOINT_REQUIRES_INVESTIGATION')
    return latest, {name:indexed[(latest,name)] for name in required}


def download(repo, asset, path):
    # Use only numeric asset IDs from this repository; no arbitrary download URL.
    identity = int(asset['id'])
    data = gh('api', f'repos/{repo}/releases/assets/{identity}', '-H', 'Accept: application/octet-stream', binary=True)
    if len(data) != asset['size']:
        raise ValueError('HOSTED_ASSET_SIZE_MISMATCH')
    Path(path).write_bytes(data)


def upload(repo, path):
    result = subprocess.run(['gh', 'release', 'upload', TAG, str(path), '--repo', repo], capture_output=True)
    if result.returncode:
        raise RuntimeError('HOSTED_CHECKPOINT_UPLOAD_FAILED')


def budget_available(state, now):
    progress = json.loads((state/'retest/backfill_progress.json').read_text())
    if progress.get('retry_not_before') and utc(progress['retry_not_before']) > now:
        return False
    rolling = json.loads((state/'cache/full_backfill_rolling_usage.json').read_text())
    daily = json.loads((state/'cache/decision_archive_usage.json').read_text())
    used = lambda hours: sum(e['units'] for e in rolling['reservations'] if utc(e['at']) > now-timedelta(hours=hours))
    day_limit = 2000 if now.date().isoformat() <= '2026-10-20' else 6000
    # Conservative preflight only; the existing worker enforces every request.
    return min(3000-used(1), 6000-used(24), day_limit-daily['days'].get(now.date().isoformat(),0)) >= 21


def verified_complete(state):
    path = state/'retest/completion.json'
    if not path.exists():
        return False
    completion = json.loads(path.read_text())
    if completion['report_file'] != completion['report_hash']+'_report.json':
        raise ValueError('HOSTED_COMPLETION_PATH_MISMATCH')
    report = json.loads((state/'retest'/completion['report_file']).read_text())
    if digest(report) != completion['report_hash'] or report.get('status') != 'offline_retest_complete':
        raise ValueError('HOSTED_COMPLETION_HASH_MISMATCH')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', args.repo) or os.environ.get('GITHUB_REPOSITORY') != args.repo:
        raise ValueError('HOSTED_REPOSITORY_MISMATCH')
    os.environ['WEATHER_BACKFILL_EXECUTOR'] = 'github_actions'
    generation, found = latest_generation(assets(args.repo))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        metadata_path = root/'prior.json'
        download(args.repo, found['checkpoint.json'], metadata_path)
        metadata = json.loads(metadata_path.read_text())
        if (metadata.get('version') != 1 or metadata.get('plan_id') != PLAN_ID
                or metadata.get('generation') != generation
                or type(metadata.get('complete')) is not bool
                or not re.fullmatch('[0-9a-f]{64}', str(metadata.get('bundle_sha256', '')))):
            raise ValueError('HOSTED_GENERATION_MISMATCH')
        if generation:
            lease_path = root/'prior-lease.json'
            download(args.repo, found['lease.json'], lease_path)
            prior_lease = json.loads(lease_path.read_text())
            if any(prior_lease.get(k) != metadata.get(k) for k in ('generation', 'parent_bundle_sha256', 'run_id')):
                raise ValueError('HOSTED_LEASE_COMMIT_MISMATCH')
        if metadata.get('complete') is True:
            print(json.dumps({'state':'already_complete', 'generation':generation, 'downloads':0}))
            return 0
        prior_zip = root/'prior.zip'
        download(args.repo, found['checkpoint.zip'], prior_zip)
        state = root/'state'
        unpack(prior_zip, metadata['bundle_sha256'], state)
        if not budget_available(state, datetime.now(timezone.utc)):
            print(json.dumps({'state':'waiting_for_budget_or_provider_cooldown', 'downloads':0}))
            return 0
        next_generation = generation+1
        lease = root/f'{next_generation:06d}-lease.json'
        lease.write_text(json.dumps({'generation':next_generation, 'parent_bundle_sha256':metadata['bundle_sha256'],
                                     'run_id':os.environ['GITHUB_RUN_ID'], 'created_at':datetime.now(timezone.utc).isoformat()}))
        upload(args.repo, lease)  # An interrupted batch must NOT restore old budgets.
        progress = run_batch(state/'archive', state/'cache', state/'market.json', state/'retest')
        bundle = root/f'{next_generation:06d}-checkpoint.zip'
        packed = pack(state/'archive', state/'cache', state/'market.json', state/'retest', bundle)
        upload(args.repo, bundle)
        # Verify the uploaded bytes before making the checkpoint authoritative.
        listed = {v['name']:v for v in assets(args.repo)}
        downloaded = root/'verification.zip'
        download(args.repo, listed[bundle.name], downloaded)
        if sha256(downloaded) != packed['sha256']:
            raise ValueError('HOSTED_UPLOAD_VERIFICATION_FAILED')
        committed = root/f'{next_generation:06d}-checkpoint.json'
        committed.write_text(json.dumps({'version':1, 'generation':next_generation, 'plan_id':PLAN_ID,
            'parent_bundle_sha256':metadata['bundle_sha256'], 'bundle_sha256':packed['sha256'],
            'complete':verified_complete(state), 'progress':progress,
            'run_id':os.environ['GITHUB_RUN_ID']}))
        upload(args.repo, committed)
        print(json.dumps({'generation':next_generation, 'state':progress['state'],
                          'requests_available':progress.get('requests_available'),
                          'location_units_this_batch':progress.get('location_units_this_batch'),
                          'retest_status':progress.get('retest_status')}))
        return 1 if progress['state']=='blocked_provider_or_evidence' else 0


if __name__ == '__main__':
    raise SystemExit(main())
