"""Shared helpers for pick listing endpoints."""
from __future__ import annotations

from backend.sports_data.mlb_fetcher import MLB_TEAM_ALIASES, canonicalise_mlb_team

PROP_BET_TYPES = (
    "draw", "total_goals", "total_runs", "btts", "spread", "corners", "cards", "shots",
    "team_shots", "team_tackles", "team_hits", "team_strikeouts",
    "team_total_goals", "team_total_runs",
    "first_half_goals", "first_five_runs",
    "player_scorer", "player_assists", "player_shots", "player_strikeouts",
    "player_goals", "player_tackles", "player_hits", "player_rbis",
)

MLB_BET_TYPES = frozenset({
    "total_runs", "first_five_runs", "team_total_runs", "team_hits", "team_strikeouts",
    "player_strikeouts", "player_hits", "player_rbis",
})


def infer_pick_sport(pick: dict) -> str | None:
    """Infer football vs mlb when match join is missing."""
    match = pick.get("matches") or {}
    if match.get("sport"):
        return match["sport"]

    bet_type = pick.get("bet_type") or "moneyline"
    if bet_type in MLB_BET_TYPES:
        return "mlb"
    if bet_type != "moneyline":
        return "football"

    winner = (pick.get("predicted_winner") or "").strip()
    if winner and canonicalise_mlb_team(winner):
        return "mlb"
    low = winner.lower()
    if low in MLB_TEAM_ALIASES or any(low in alias for alias in MLB_TEAM_ALIASES):
        return "mlb"
    return "football"


def filter_picks_by_sport(rows: list[dict], sport: str | None, *, limit: int | None = None) -> list[dict]:
    if not sport:
        return rows[:limit] if limit else rows
    filtered = [r for r in rows if infer_pick_sport(r) == sport]
    return filtered[:limit] if limit else filtered
