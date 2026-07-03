"""Learn Polymarket MLB in-game auto-bet thresholds from outcomes (stored under ``data/``)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import data_paths as dp

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "min_run_diff": 6.0,
    "min_inning": 7.0,
    "max_implied_yes": 0.94,
    "min_implied_yes": 0.82,
    "outcomes_n": 0,
    "wins": 0,
}

_BOUNDS: dict[str, tuple[float, float]] = {
    "min_run_diff": (2.0, 12.0),
    "min_inning": (5.0, 8.5),
    "max_implied_yes": (0.88, 0.985),
    "min_implied_yes": (0.72, 0.92),
}


def _path() -> str:
    return os.path.join(dp.data_dir(), "mlb_ingame_learning.json")


def load_state() -> dict[str, Any]:
    path = _path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return dict(_DEFAULTS)
        out = dict(_DEFAULTS)
        out.update(data)
        return out
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULTS)


def save_state(state: dict[str, Any]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def get_thresholds() -> dict[str, float]:
    s = load_state()
    return {
        "min_run_diff": float(s["min_run_diff"]),
        "min_inning": float(s["min_inning"]),
        "max_implied_yes": float(s["max_implied_yes"]),
        "min_implied_yes": float(s["min_implied_yes"]),
    }


def summary_line() -> str:
    s = load_state()
    n = int(s.get("outcomes_n") or 0)
    w = int(s.get("wins") or 0)
    th = get_thresholds()
    return (
        f"diff≥{th['min_run_diff']:.1f} inn≥{th['min_inning']:.1f} "
        f"imp∈[{th['min_implied_yes']:.2f},{th['max_implied_yes']:.2f}] "
        f"(n={n} W={w})"
    )


def record_outcome(won: bool) -> None:
    """Nudge thresholds after an in-game auto position resolves."""
    state = load_state()
    lr = 0.45
    state["outcomes_n"] = int(state.get("outcomes_n") or 0) + 1
    state["wins"] = int(state.get("wins") or 0) + (1 if won else 0)

    def _b(k: str, v: float) -> float:
        lo, hi = _BOUNDS[k]
        return max(lo, min(hi, v))

    if won:
        state["min_run_diff"] = _b(
            "min_run_diff", float(state["min_run_diff"]) - lr * 0.35
        )
        state["min_inning"] = _b("min_inning", float(state["min_inning"]) - lr * 0.2)
        state["max_implied_yes"] = _b(
            "max_implied_yes", float(state["max_implied_yes"]) + lr * 0.008
        )
        state["min_implied_yes"] = _b(
            "min_implied_yes", float(state["min_implied_yes"]) - lr * 0.006
        )
    else:
        state["min_run_diff"] = _b(
            "min_run_diff", float(state["min_run_diff"]) + lr * 0.5
        )
        state["min_inning"] = _b(
            "min_inning", float(state["min_inning"]) + lr * 0.25
        )
        state["max_implied_yes"] = _b(
            "max_implied_yes", float(state["max_implied_yes"]) - lr * 0.01
        )
        state["min_implied_yes"] = _b(
            "min_implied_yes", float(state["min_implied_yes"]) + lr * 0.01
        )

    save_state(state)
    logger.info(
        "mlb_ingame_learning: won=%s → diff≥%.2f inn≥%.2f imp[%.3f,%.3f] (sample %d)",
        won,
        float(state["min_run_diff"]),
        float(state["min_inning"]),
        float(state["min_implied_yes"]),
        float(state["max_implied_yes"]),
        int(state["outcomes_n"]),
    )
