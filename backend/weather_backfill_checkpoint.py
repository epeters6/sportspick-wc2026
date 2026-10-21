"""Portable, hash-checked backfill evidence; never bootstrap a missing ledger."""
from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from backend.ml.weather_learning.dataset import digest, utc

PLAN_ID = "a358482b0e667499ce9da4bf18131c8aa95972e37820e607cab4042a79667026"
OWNER_FILE = "full_backfill_owner.json"
# Immutable historical dataset revisions grow across batches; preserve them all.
# The compressed release asset still stays below GitHub's individual asset cap.
MAX_UNPACKED = 8_000_000_000
MAX_BUNDLE = 2_000_000_000


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def validate_owner(cache, executor="local"):
    path = Path(cache)/OWNER_FILE
    if not path.exists():
        if executor != "local":
            raise ValueError("HOSTED_BACKFILL_REQUIRES_EXPLICIT_HANDOFF")
        return
    owner = json.loads(path.read_text(encoding="utf-8"))
    if (owner.get("version") != 1 or owner.get("plan_id") != PLAN_ID
            or owner.get("executor") not in ("local", "github_actions") or owner["executor"] != executor):
        raise ValueError("BACKFILL_EXECUTION_OWNER_MISMATCH")


def validate_state(archive, cache, retest):
    archive, cache, retest = Path(archive), Path(cache), Path(retest)
    if any(cache.glob("*.lock")):
        raise ValueError("BACKFILL_LOCK_PRESENT")
    plan = json.loads((archive/'plan.json').read_text(encoding="utf-8"))
    if plan.get('plan_id') != PLAN_ID or digest({k:v for k,v in plan.items() if k!='plan_id'}) != PLAN_ID:
        raise ValueError("CHECKPOINT_PLAN_MISMATCH")
    latest = json.loads((archive/'latest.json').read_text(encoding="utf-8"))
    progress = json.loads((retest/'backfill_progress.json').read_text(encoding="utf-8"))
    if latest['plan_id'] != PLAN_ID or progress['plan_id'] != PLAN_ID or latest['dataset_hash'] != progress['dataset_hash']:
        raise ValueError("CHECKPOINT_PROGRESS_MISMATCH")
    if latest['dataset_file'] != latest['dataset_hash']+'_dataset.json':
        raise ValueError("CHECKPOINT_DATASET_PATH_MISMATCH")
    if digest(json.loads((archive/latest['dataset_file']).read_text(encoding="utf-8"))) != latest['dataset_hash']:
        raise ValueError("CHECKPOINT_DATASET_HASH_MISMATCH")
    daily = json.loads((cache/'decision_archive_usage.json').read_text(encoding="utf-8"))
    rolling = json.loads((cache/'full_backfill_rolling_usage.json').read_text(encoding="utf-8"))
    if daily.get('version') != 1 or not isinstance(daily.get('days'), dict) or any(type(v) is not int or v < 0 for v in daily['days'].values()):
        raise ValueError("CHECKPOINT_DAILY_LEDGER_INVALID")
    if rolling.get('version') != 1 or not isinstance(rolling.get('reservations'), list):
        raise ValueError("CHECKPOINT_ROLLING_LEDGER_INVALID")
    for value in rolling['reservations']:
        if type(value.get('units')) is not int or value['units'] < 0 or utc(value['at']) > datetime.now(timezone.utc):
            raise ValueError("CHECKPOINT_ROLLING_LEDGER_INVALID")
    return progress


def pack(archive, cache, market_history, retest, destination):
    """Only explicitly selected weather evidence enters this portable bundle."""
    archive, cache, market_history, retest = map(Path, (archive, cache, market_history, retest))
    progress = validate_state(archive, cache, retest)
    validate_owner(cache, "github_actions")
    files = {}
    for prefix, root in (("archive", archive), ("cache", cache), ("retest", retest)):
        for path in root.rglob('*'):
            if path.is_symlink():
                raise ValueError("CHECKPOINT_SYMLINK_REJECTED")
            if path.is_file():
                if path.suffix not in ('.json', '.gz', '.jsonl', '.md'):
                    raise ValueError("UNEXPECTED_CHECKPOINT_FILE")
                files[prefix+'/'+path.relative_to(root).as_posix()] = path
    files['market.json'] = market_history
    if not market_history.is_file() or market_history.is_symlink():
        raise ValueError("CHECKPOINT_MARKET_HISTORY_MISSING")
    manifest = {"version": 1, "plan_id": PLAN_ID, "executor": "github_actions", "progress": progress,
                "files": {name:{"sha256":sha256(path), "size":path.stat().st_size} for name,path in sorted(files.items())}}
    if sum(v['size'] for v in manifest['files'].values()) > MAX_UNPACKED:
        raise ValueError("CHECKPOINT_TOO_LARGE")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
        for name, path in sorted(files.items()):
            if sha256(path) != manifest['files'][name]['sha256']:
                raise ValueError("CHECKPOINT_CHANGED_DURING_PACK")
            bundle.write(path, name)
    if destination.stat().st_size > MAX_BUNDLE:
        raise ValueError("CHECKPOINT_COMPRESSED_BUNDLE_TOO_LARGE")
    return {"sha256": sha256(destination), "size": destination.stat().st_size, "progress": progress}


def unpack(bundle_path, expected_hash, destination):
    if Path(bundle_path).stat().st_size > MAX_BUNDLE:
        raise ValueError("CHECKPOINT_COMPRESSED_BUNDLE_TOO_LARGE")
    if not re.fullmatch('[0-9a-f]{64}', expected_hash) or sha256(bundle_path) != expected_hash:
        raise ValueError("CHECKPOINT_BUNDLE_HASH_MISMATCH")
    destination = Path(destination)
    if destination.exists():
        raise ValueError("CHECKPOINT_DESTINATION_MUST_BE_NEW")
    with zipfile.ZipFile(bundle_path) as bundle:
        infos = bundle.infolist()
        if len({i.filename for i in infos}) != len(infos) or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise ValueError("CHECKPOINT_DUPLICATE_OR_OVERSIZED")
        if bundle.getinfo('manifest.json').file_size > 10_000_000:
            raise ValueError("CHECKPOINT_MANIFEST_TOO_LARGE")
        manifest = json.loads(bundle.read('manifest.json'))
        if manifest.get('version') != 1 or manifest.get('plan_id') != PLAN_ID or manifest.get('executor') != 'github_actions':
            raise ValueError("CHECKPOINT_MANIFEST_INVALID")
        if set(i.filename for i in infos) != {'manifest.json', *manifest['files']}:
            raise ValueError("CHECKPOINT_FILE_SET_MISMATCH")
        for name, value in manifest['files'].items():
            path = PurePosixPath(name)
            info = bundle.getinfo(name)
            if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
                    or path.parts[0] not in ('archive', 'cache', 'retest', 'market.json')
                    or stat.S_ISLNK(info.external_attr >> 16) or info.file_size != value['size']):
                raise ValueError("UNSAFE_CHECKPOINT_MEMBER")
            data = bundle.read(name)
            if hashlib.sha256(data).hexdigest() != value['sha256']:
                raise ValueError("CHECKPOINT_MEMBER_HASH_MISMATCH")
        destination.mkdir(parents=True)
        for name in manifest['files']:
            path = destination.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(bundle.read(name))
    validate_owner(destination/'cache', 'github_actions')
    validate_state(destination/'archive', destination/'cache', destination/'retest')
    return manifest
