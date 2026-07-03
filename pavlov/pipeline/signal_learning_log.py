"""Append forecast snapshots for post-settlement learning (skips + auto-rejects).

Rows are written under ``logs/signals.json`` (Kalshi) or ``logs_poly/signals.json`` (Poly),
which honor ``STATE_DIRECTORY`` / Railway volume — same paths as the rest of the bot state.
See ``data_paths.state_root()`` and ``warn_if_learning_state_ephemeral``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# learning_loop processes these actions for ensemble bias, calibration, soft station nudge
LEARNING_ACTIONS = frozenset({"skip", "signal_watch"})


def _load(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)


def _row_key(sig: dict) -> tuple[str, str, str]:
    return (
        str(sig.get("ticker", "")),
        str(sig.get("market_date", "")),
        str(sig.get("recommended_side", "")),
    )


def append_learning_record(
    signals_path: str,
    signal: dict,
    *,
    action: str,
    learn_reason: str = "",
    learn_source: str = "",
    venue: str = "",
) -> bool:
    """Append one row to *signals_path* if no pending learning row exists for the same key.

    Returns True if a new row was appended.
    """
    if action not in LEARNING_ACTIONS:
        raise ValueError(f"action must be one of {sorted(LEARNING_ACTIONS)}, got {action!r}")

    rows = _load(signals_path)
    want = _row_key(signal)
    if not want[0]:
        return False

    for r in rows:
        if r.get("action") not in LEARNING_ACTIONS:
            continue
        if _row_key(r) == want:
            return False

    row: dict[str, Any] = {
        "venue":            venue or signal.get("venue") or "",
        "ticker":           signal.get("ticker", ""),
        "city":             signal.get("city", ""),
        "metric":           signal.get("metric", ""),
        "direction":        signal.get("direction", ""),
        "threshold_f":      signal.get("threshold_f"),
        "threshold_lo":     signal.get("threshold_lo"),
        "threshold_hi":     signal.get("threshold_hi"),
        "recommended_side": signal.get("recommended_side", ""),
        "market_date":      signal.get("market_date", ""),
        "station":          signal.get("station", ""),
        "nws_predicted":    signal.get("nws_predicted"),
        "ensemble_mean":    signal.get("ensemble_mean"),
        "ensemble_spread":  signal.get("ensemble_spread"),
        "edge":             signal.get("edge"),
        "model_prob":       signal.get("model_prob"),
        "implied_prob":     signal.get("implied_prob"),
        "action":           action,
        "learn_reason":     learn_reason,
        "learn_source":     learn_source,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "actual_temp_f":    None,
        "bias_updated":     False,
        "station_nudged":   False,
    }
    rows.append(row)
    _save(signals_path, rows)
    logger.info(
        "SignalLearningLog: %s recorded for %s (%s) — %s",
        action,
        signal.get("ticker"),
        learn_source,
        learn_reason[:120] if learn_reason else "—",
    )
    return True
