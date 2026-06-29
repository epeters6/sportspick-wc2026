"""Format a structured pick for display."""
from __future__ import annotations

from typing import Any


def format_pick_label(pick: dict[str, Any]) -> str:
    """Human-readable pick label including subject + direction + line."""
    pw = pick.get("predicted_winner") or ""
    bt = pick.get("bet_type") or "moneyline"
    line = pick.get("bet_line")
    subject = pick.get("bet_subject")

    if bt == "moneyline":
        return pw
    if bt == "draw":
        return "Draw"
    if bt == "player_scorer":
        return f"{subject or pw} to score"
    if bt in ("player_assists",):
        return f"{subject or pw} assist"
    if pw in ("over", "under", "yes", "no"):
        dir_label = pw.upper() if len(pw) <= 5 else pw
        parts: list[str] = []
        if subject and subject not in ("match",):
            parts.append(str(subject))
        parts.append(dir_label)
        if line:
            parts.append(str(line))
        stat = _stat_suffix(bt)
        if stat:
            parts.append(stat)
        return " ".join(parts)
    if subject and line:
        return f"{subject} — {pw} {line}"
    return pw or "—"


def _stat_suffix(bet_type: str) -> str:
    suffixes = {
        "total_goals": "goals",
        "total_runs": "runs",
        "team_total_goals": "team goals",
        "team_total_runs": "team runs",
        "team_shots": "shots",
        "team_tackles": "tackles",
        "player_shots": "shots",
        "player_strikeouts": "K",
        "first_half_goals": "1H goals",
        "first_five_runs": "F5 runs",
        "corners": "corners",
        "cards": "cards",
        "shots": "shots",
    }
    return suffixes.get(bet_type, "")
