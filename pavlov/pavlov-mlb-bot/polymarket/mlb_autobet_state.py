"""Paths and autobet suppress file under the MLB bot state root (``STATE_DIRECTORY``)."""

from __future__ import annotations

import os
import time


def mlb_bot_root() -> str:
    import data_paths as dp

    return dp.state_root()


def mlb_positions_path() -> str:
    return os.path.join(mlb_bot_root(), "logs", "positions.json")


def mlb_signals_path() -> str:
    return os.path.join(mlb_bot_root(), "logs", "signals.json")


def mlb_pending_mlb_poly_path() -> str:
    return os.path.join(mlb_bot_root(), "data", "pending_mlb_poly_signals.json")


def mlb_autobet_suppress_path() -> str:
    return os.path.join(mlb_bot_root(), "data", "mlb_autobet_suppress_until.txt")


def mlb_ingame_learning_path() -> str:
    return os.path.join(mlb_bot_root(), "data", "mlb_ingame_learning.json")


def autobet_suppressed(now: float | None = None) -> bool:
    path = mlb_autobet_suppress_path()
    now = now if now is not None else time.time()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            until = float(fh.read().strip())
        return now < until
    except (FileNotFoundError, ValueError, OSError):
        return False


def arm_autobet_suppress(seconds: float | None = None) -> None:
    raw = os.environ.get("POLY_MLB_AUTOBET_ARM_GRACE_SECONDS", "").strip()
    if seconds is not None:
        secs = float(seconds)
    elif raw:
        secs = float(raw)
    else:
        secs = 120.0
    path = mlb_autobet_suppress_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    until = time.time() + max(5.0, secs)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(until))
