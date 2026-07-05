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
    "POLY_MLB_PREGAME_AUTO_ENABLED": {
        "type": int,
        "default": 1,
        "description": "1 = run pregame MLB Poly auto-bet cycles (also requires master MLB arm on)",
    },
    # MLB pregame auto-bet (confidence-gated; edge required by default)
    "POLY_MLB_AUTO_MIN_MODEL_CONFIDENCE": {
        "type": float,
        "default": 0.65,
        "description": "Pregame auto-bet only if model_confidence >= this (0–1 blend agreement)",
    },
    "POLY_MLB_AUTO_MIN_EDGE": {
        "type": float,
        "default": 0.08,
        "description": "Also require |edge| >= this for pregame auto-bet (set 0 to disable)",
    },
    "POLY_MLB_AUTO_MAX_KELLY_DOLLARS": {
        "type": float,
        "default": 1.5,
        "description": "Cap estimated stake (USD) before sizing MLB pregame auto orders",
    },
    "POLY_MLB_AUTO_MAX_CONTRACTS": {
        "type": int,
        "default": 1,
        "description": "Hard cap on contracts per MLB pregame auto-bet (min-notional skip if higher needed)",
    },
    "POLY_MLB_AUTO_MAX_BETS_PER_ET_DAY": {
        "type": int,
        "default": 3,
        "description": "Max MLB **pregame** auto positions per America/New_York calendar day",
    },
    "POLY_MLB_AUTO_REQUIRE_STRONG": {
        "type": int,
        "default": 1,
        "description": "1 = only signal_strength 'strong' for pregame auto (combined edge + confidence)",
    },
    "POLY_MLB_AUTO_MAX_IMPLIED": {
        "type": float,
        "default": 0.85,
        "description": "Block pregame auto-bet when implied yes_price is >= this (avoid fading huge favorites)",
    },
    "POLY_MLB_AUTO_MIN_IMPLIED": {
        "type": float,
        "default": 0.15,
        "description": "Block pregame auto-bet when implied yes_price is <= this (avoid backing huge underdogs)",
    },
    "MLB_KELLY_FRACTION": {
        "type": float,
        "default": 0.12,
        "description": "MLB-specific Kelly fraction. Overrides KELLY_FRACTION for MLB sizing.",
    },
    "MLB_MAX_BET_BANKROLL_FRAC": {
        "type": float,
        "default": 0.04,
        "description": "Max single MLB bet as fraction of bankroll (was 0.08 / 8% in older code).",
    },
    "MLB_EXTREME_IMPLIED": {
        "type": float,
        "default": 0.92,
        "description": "Treat markets with implied >= this (or <= 1 − this) as extreme tails.",
    },
    "MLB_EXTREME_MIN_EDGE": {
        "type": float,
        "default": 0.25,
        "description": "Min |edge| required on extreme-tail markets to keep the signal.",
    },
    "MLB_EXTREME_MIN_CONFIDENCE": {
        "type": float,
        "default": 0.70,
        "description": "Min model_confidence required on extreme-tail markets to keep the signal.",
    },
    "MLB_PENDING_AUTO_SKIP_HOURS": {
        "type": float,
        "default": 1.0,
        "description": "If no BET/SKIP on a Discord MLB alert within this many hours, log it as skip for learning (0 = off).",
    },
    "MLB_PENDING_AUTO_SKIP_CHECK_MINUTES": {
        "type": int,
        "default": 10,
        "description": "How often to scan pending MLB Discord alerts for the auto-skip window.",
    },
    # In-game auto-bet (live score + Polymarket; learned thresholds in data/mlb_ingame_learning.json)
    "POLY_MLB_INGAME_ENABLED": {
        "type": int,
        "default": 0,
        "description": "1 = poll live games and place small in-game auto-bets when gates pass",
    },
    "POLY_MLB_INGAME_POLL_MINUTES": {
        "type": int,
        "default": 12,
        "description": "Minutes between in-game Polymarket checks (while bot runs)",
    },
    "POLY_MLB_INGAME_MAX_KELLY_DOLLARS": {
        "type": float,
        "default": 2.0,
        "description": "Cap est. stake (USD) for each in-game auto order",
    },
    "POLY_MLB_INGAME_MAX_CONTRACTS": {
        "type": int,
        "default": 1,
        "description": "Hard cap contracts per in-game auto-bet",
    },
    "POLY_MLB_INGAME_MAX_BETS_PER_ET_DAY": {
        "type": int,
        "default": 5,
        "description": "Max in-game auto positions per ET calendar day (also 1 open + 1/day per game)",
    },
    "POLY_MLB_AUTOBET_ARM_GRACE_SECONDS": {
        "type": float,
        "default": 120.0,
        "description": "After turning MLB auto ON via / commands, skip auto orders for this long (shared suppress file).",
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
        if os.environ.get("PAVLOV_BYPASS_CONFIG", "0") == "1":
            for k in _REQUIRED:
                if k not in cfg:
                    cfg[k] = "" if _REQUIRED[k]["type"] == str else 0
        else:
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
