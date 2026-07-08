"""
Load environment for pavlov-mlb-bot.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ROOT, ".env"))

_REQUIRED: dict[str, dict] = {
    "DISCORD_BOT_TOKEN": {"type": str, "description": "Discord bot token"},
    "DISCORD_CHANNEL_ID": {
        "type": int,
        "description": "Discord channel ID for MLB / Polymarket alerts",
    },
    "MIN_EDGE_THRESHOLD": {
        "type": float,
        "description": "Minimum |edge| to surface a signal (probability units)",
    },
    "KELLY_FRACTION": {"type": float, "description": "Fraction of Kelly for sizing"},
    "CHECK_INTERVAL_MINUTES": {
        "type": int,
        "description": "Legacy interval (unused by main.py run — ET schedule is primary)",
    },
}

_OPTIONAL: dict[str, dict] = {
    "MLB_API_BASE": {
        "type": str,
        "default": "https://statsapi.mlb.com/api/v1",
        "description": "MLB Stats API base URL",
    },
    "MLB_API_USER_AGENT": {
        "type": str,
        "default": "pavlov-mlb-bot/1.0",
        "description": "User-Agent for MLB Stats API HTTP requests",
    },
    "POLY_KEY_ID": {
        "type": str,
        "default": "",
        "description": "Polymarket US API key id (optional)",
    },
    "POLY_SECRET_KEY": {
        "type": str,
        "default": "",
        "description": "Polymarket US API secret (optional)",
    },
    "POLYMARKET_KEY_ID": {"type": str, "default": "", "description": "Alias for POLY_KEY_ID"},
    "POLYMARKET_SECRET_KEY": {
        "type": str,
        "default": "",
        "description": "Alias for POLY_SECRET_KEY",
    },
    "POLYMARKET_API_KEY": {
        "type": str,
        "default": "",
        "description": "Legacy single field; prefer POLY_KEY_ID + POLY_SECRET_KEY",
    },
    "STATE_DIRECTORY": {
        "type": str,
        "default": "",
        "description": "Persistent root for logs/ and data/ (Railway volume path)",
    },
    "AUTO_BET_PRICE_BUFFER_CENTS": {
        "type": int,
        "default": 5,
        "description": "Kalshi limit buffer (cents) if using Kalshi orders",
    },
    "DISCORD_POLY_ID": {
        "type": int,
        "default": 0,
        "description": "Separate Discord channel for Polymarket (0 = use DISCORD_CHANNEL_ID)",
    },
    "DISCORD_POLY_MLB_ID": {
        "type": int,
        "default": 0,
        "description": "Discord channel for MLB Polymarket signals (0 = DISCORD_POLY_ID or main)",
    },
    "POLY_AUTO_BET_ENABLED": {
        "type": int,
        "default": 0,
        "description": "Enable Polymarket US auto-bet (1/true)",
    },
    "POLY_MLB_AUTO_BET_ENABLED": {
        "type": int,
        "default": 0,
        "description": "MLB Polymarket auto-bet (0 = fall back to POLY_AUTO_BET_ENABLED)",
    },
}


def _cast(key: str, raw: str, target_type: type):
    try:
        return target_type(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"CONFIG ERROR – '{key}' cannot convert to {target_type.__name__!r}: {raw!r}"
        ) from exc


def truthy_config_int(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    if isinstance(val, int):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def _load_config() -> dict:
    missing = []
    cfg: dict = {}

    for key, meta in _REQUIRED.items():
        raw = os.environ.get(key, "").strip()
        if not raw:
            missing.append(f"  • {key} – {meta['description']}")
        else:
            cfg[key] = _cast(key, raw, meta["type"])

    if missing:
        raise EnvironmentError(
            "pavlov-mlb-bot: missing required environment variables:\n"
            + "\n".join(missing)
        )

    for key, meta in _OPTIONAL.items():
        raw = os.environ.get(key, "").strip()
        if raw:
            cfg[key] = _cast(key, raw, meta["type"])
        else:
            cfg[key] = meta["default"]

    if not str(cfg.get("POLY_KEY_ID") or "").strip():
        alt = str(cfg.get("POLYMARKET_KEY_ID") or os.environ.get("POLYMARKET_KEY_ID", "")).strip()
        if alt:
            cfg["POLY_KEY_ID"] = alt

    if not str(cfg.get("POLY_SECRET_KEY") or "").strip():
        alt = str(
            cfg.get("POLYMARKET_SECRET_KEY") or os.environ.get("POLYMARKET_SECRET_KEY", "")
        ).strip()
        if alt:
            cfg["POLY_SECRET_KEY"] = alt

    if not str(cfg.get("STATE_DIRECTORY") or "").strip():
        rvm = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        if rvm:
            cfg["STATE_DIRECTORY"] = rvm

    return cfg


CONFIG: dict = _load_config()
