"""Dedupe MLB Discord alerts: one pregame + one in-game post per game per day key."""

from __future__ import annotations

_MAX_ALERT_CACHE = 500
_MLB_ALERT_KEYS: set[str] = set()


def dedupe_key(sig: dict) -> str:
    mode = "ingame" if sig.get("ingame_context") else "pregame"
    gid = sig.get("game_id")
    gd = sig.get("game_date")
    if gid is not None:
        return f"{mode}|{gid}|{gd}"
    ha = sig.get("home_team_abbr") or ""
    aa = sig.get("away_team_abbr") or ""
    return f"{mode}|{ha}|{aa}|{gd}"


def try_mark_alert(sig: dict) -> bool:
    """Return True if this alert should be sent (first time for this dedupe key)."""
    key = dedupe_key(sig)
    if key in _MLB_ALERT_KEYS:
        return False
    _MLB_ALERT_KEYS.add(key)
    while len(_MLB_ALERT_KEYS) > _MAX_ALERT_CACHE:
        _MLB_ALERT_KEYS.pop()
    return True
