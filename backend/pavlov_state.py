"""
Sync Pavlov JSON state between local STATE_DIRECTORY and Supabase.

Used by GitHub Actions: pull before cycle-once, push after.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from backend.db import get_db

# Relative paths under each namespace root to persist
WEATHER_STATE_DIRS = ("logs", "data", "logs_poly", "data_poly")
MLB_STATE_DIRS = ("logs", "data")


def _read_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    if path.suffix == ".jsonl":
        return {"_jsonl": text}
    return {"_raw": text}


def _write_file(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict) and "_jsonl" in content:
        path.write_text(content["_jsonl"], encoding="utf-8")
    elif isinstance(content, dict) and "_raw" in content:
        path.write_text(content["_raw"], encoding="utf-8")
    else:
        path.write_text(json.dumps(content, indent=2), encoding="utf-8")


def _collect_files(root: Path, subdirs: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for fp in base.rglob("*"):
            if not fp.is_file():
                continue
            rel = str(fp.relative_to(root)).replace("\\", "/")
            try:
                out[rel] = _read_file(fp)
            except Exception as exc:
                logger.warning(f"pavlov_state: skip {rel}: {exc}")
    return out


def pull_namespace(namespace: str, root: Path, subdirs: tuple[str, ...]) -> int:
    """Download stored files into *root*."""
    db = get_db()
    rows = (
        db.table("pavlov_state")
        .select("file_path, content")
        .eq("namespace", namespace)
        .execute()
        .data or []
    )
    count = 0
    for row in rows:
        rel = row.get("file_path")
        if not rel:
            continue
        fp = root / rel
        try:
            _write_file(fp, row.get("content") or {})
            count += 1
        except Exception as exc:
            logger.warning(f"pavlov_state pull: failed {rel}: {exc}")
    logger.info(f"pavlov_state: pulled {count} files → {root} [{namespace}]")
    return count


def push_namespace(namespace: str, root: Path, subdirs: tuple[str, ...]) -> int:
    """Upload local files under *root* to Supabase."""
    db = get_db()
    files = _collect_files(root, subdirs)
    if not files:
        return 0
    rows = [
        {"namespace": namespace, "file_path": rel, "content": content}
        for rel, content in files.items()
    ]
    db.table("pavlov_state").upsert(rows, on_conflict="namespace,file_path").execute()
    logger.info(f"pavlov_state: pushed {len(rows)} files ← {root} [{namespace}]")
    return len(rows)


def pull_weather(state_root: Path) -> int:
    return pull_namespace("weather", state_root, WEATHER_STATE_DIRS)


def push_weather(state_root: Path) -> int:
    return push_namespace("weather", state_root, WEATHER_STATE_DIRS)


def pull_mlb(state_root: Path) -> int:
    return pull_namespace("mlb", state_root, MLB_STATE_DIRS)


def push_mlb(state_root: Path) -> int:
    return push_namespace("mlb", state_root, MLB_STATE_DIRS)


def default_weather_state_dir() -> Path:
    env = os.environ.get("PAVLOV_STATE_DIRECTORY", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "pavlov" / "_state"


def default_mlb_state_dir() -> Path:
    base = default_weather_state_dir()
    return base / "mlb_bot"
