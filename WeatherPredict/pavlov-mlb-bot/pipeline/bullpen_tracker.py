"""Bullpen fatigue from ``mlb_client.get_bullpen_usage``."""

from __future__ import annotations

from typing import Any

from pipeline import mlb_client


def _label_for_fatigue(fatigue: float) -> str:
    if fatigue <= 15:
        return "fresh"
    if fatigue <= 35:
        return "normal"
    if fatigue <= 55:
        return "tired"
    return "exhausted"


def analyze_bullpen(team_id: int, days: int = 7) -> dict[str, Any]:
    usage = mlb_client.get_bullpen_usage(int(team_id), days=int(days))

    ti = float(usage.get("total_innings") or 0)
    r2 = int(usage.get("relievers_used_2_consecutive") or 0)
    closer_ok = bool(usage.get("closer_available"))
    hl_ok = bool(usage.get("high_leverage_available"))

    fatigue = 0.0
    if ti > 14:
        fatigue += 15
    if ti > 20:
        fatigue += 20
    if r2 > 3:
        fatigue += 15
    if not closer_ok:
        fatigue += 25
    if not hl_ok:
        fatigue += 15

    fatigue = min(100.0, fatigue)
    strength = max(0.0, 100.0 - fatigue)

    return {
        "fatigue_score": round(fatigue, 2),
        "strength": round(strength, 2),
        "label": _label_for_fatigue(fatigue),
        "closer_available": closer_ok,
        "high_leverage_available": hl_ok,
        "usage": usage,
    }
