"""Umpire run-environment prior from a rolling game cache."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import data_paths as dp
from pipeline import mlb_client

logger = logging.getLogger(__name__)

# Persist on STATE_DIRECTORY / Railway volume (same as games_cache, forecast_cache, …)
_CACHE_PATH = os.path.join(dp.data_dir(), "umpire_cache.json")
_WINDOW = 50
DEFAULT_RUNS_PER_GAME = 9.0

_cache: dict[str, Any] | None = None


def load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        _cache = raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = {}
    return _cache


def _save_cache(data: dict) -> None:
    global _cache
    _cache = data
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def _roll_avg(runs: list[float]) -> float:
    if not runs:
        return DEFAULT_RUNS_PER_GAME
    return round(sum(runs) / len(runs), 3)


def get_run_factor(game_id: int) -> float:
    ump = mlb_client.get_umpire_for_game(int(game_id))
    if not ump:
        return 1.0
    data = load_cache().get(ump) or {}
    avg = float(data.get("avg_runs_per_game", DEFAULT_RUNS_PER_GAME))
    return round(avg / DEFAULT_RUNS_PER_GAME, 4)


def update_after_game(game_id: int, total_runs: int | float) -> dict[str, Any] | None:
    """Append ``total_runs`` to umpire's rolling window and persist."""
    ump = mlb_client.get_umpire_for_game(int(game_id))
    if not ump:
        logger.info("umpire_tracker: no HP ump for game %s — skip update.", game_id)
        return None

    c = dict(load_cache())
    rec = dict(c.get(ump) or {})
    hist = list(rec.get("recent_runs") or [])
    try:
        tr = float(total_runs)
    except (TypeError, ValueError):
        return None
    hist.append(tr)
    if len(hist) > _WINDOW:
        hist = hist[-_WINDOW:]
    rec["recent_runs"] = hist
    rec["avg_runs_per_game"] = _roll_avg(hist)
    rec["games_tracked"] = len(hist)
    c[ump] = rec
    _save_cache(c)
    return rec
