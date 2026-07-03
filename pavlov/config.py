"""
config.py – Central configuration loader for pavlov-weather-bot.

Loads all variables from the .env file (or environment), validates that
every required key is present, and exports a single CONFIG dict that the
rest of the application imports.
"""

import os
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env from the project root (one directory above this file)
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ROOT, ".env"))

# ---------------------------------------------------------------------------
# Required keys and their types / descriptions
# ---------------------------------------------------------------------------
_REQUIRED: dict[str, dict] = {
    "KALSHI_API_KEY": {
        "type": str,
        "description": (
            "Kalshi API key UUID – generate one at "
            "https://trading.kalshi.com/settings/api"
        ),
    },
    "KALSHI_PRIVATE_KEY_PATH": {
        "type": str,
        "description": (
            "Path to the RSA private key PEM file (local dev). "
            "On cloud hosts set KALSHI_PRIVATE_KEY_B64 instead — "
            "if that env var is set this field is ignored."
        ),
    },
    "DISCORD_BOT_TOKEN": {
        "type": str,
        "description": "Discord bot token from the Developer Portal",
    },
    "DISCORD_CHANNEL_ID": {
        "type": int,
        "description": "Numeric ID of the Discord channel to post Kalshi alerts in",
    },
    "MIN_EDGE_THRESHOLD": {
        "type": float,
        "description": (
            "Minimum edge (forecast_prob − market_price) required to "
            "generate a signal (e.g. 0.15 = 15 cents)"
        ),
    },
    "KELLY_FRACTION": {
        "type": float,
        "description": (
            "Fraction of the full Kelly criterion to stake (e.g. 0.25 = "
            "quarter-Kelly)"
        ),
    },
    "CHECK_INTERVAL_MINUTES": {
        "type": int,
        "description": "How often (in minutes) the main loop re-checks markets",
    },
}

# ---------------------------------------------------------------------------
# Optional keys (have sensible defaults, not required to be set)
# ---------------------------------------------------------------------------
_OPTIONAL: dict[str, dict] = {
    "OWM_API_KEY": {
        "type": str,
        "default": "",
        "description": "OpenWeatherMap API key (free tier). Enables second-source consensus.",
    },
    "MAX_DAILY_LOSS": {
        "type": float,
        "default": 5.0,
        "description": "Maximum dollars to lose per calendar day before pausing trading.",
    },
    # ── Auto-bet (autonomous trading) ─────────────────────────────────────
    # When AUTO_BET_ENABLED is true the bot will place the order itself —
    # without waiting for a Discord button click — but only on signals that
    # pass ALL the strict criteria below.  The defaults are intentionally
    # conservative so very few signals qualify.
    "AUTO_BET_ENABLED": {
        "type": int,           # 0 or 1 (env vars are strings; bool is unreliable)
        "default": 0,
        "description": "Enable autonomous order placement on extreme-edge signals (0=off, 1=on).",
    },
    "AUTO_BET_MIN_EDGE": {
        "type": float,
        "default": 0.05,
        "description": "Minimum edge before auto-bet fires (5¢ floor — effectively any profit).",
    },
    "AUTO_BET_MAX_SPREAD": {
        "type": float,
        "default": 2.5,
        "description": "Maximum ensemble spread in °F to allow auto-bet (smaller = more confident).",
    },
    "AUTO_BET_MIN_MARGIN": {
        "type": float,
        "default": 2.0,
        "description": "Minimum forecast margin from threshold in °F to allow auto-bet.",
    },
    "AUTO_BET_MAX_HORIZON_DAYS": {
        "type": int,
        "default": 1,
        "description": "Max days_out (0=today, 1=tomorrow) eligible for auto-bet.",
    },
    "AUTO_BET_MAX_PER_DAY": {
        "type": int,
        "default": 8,
        "description": "Hard daily cap on auto-bets (calendar day).",
    },
    "AUTO_BET_MAX_DOLLARS_PER_DAY": {
        "type": float,
        "default": 15.00,
        "description": "Hard daily dollar cap on total auto-bet cost.",
    },
    "AUTO_BET_MIN_PROB_YES": {
        "type": float,
        "default": 0.85,
        "description": "Minimum model_prob for YES auto-bets (0.85 = 85% confident).",
    },
    "AUTO_BET_MAX_PROB_NO": {
        "type": float,
        "default": 0.15,
        "description": "Maximum model_prob for NO auto-bets (0.15 = 85% confident NO).",
    },
    "AUTO_BET_PRICE_BUFFER_CENTS": {
        "type": int,
        "default": 5,
        "description": (
            "Kalshi auto-bet only: add this many cents above the rounded ask-implied price "
            "so limit orders fill instead of resting (1 = old behavior; default 5¢)."
        ),
    },
    # ── Polymarket US (optional) ───────────────────────────────────────────
    "POLY_KEY_ID": {
        "type": str,
        "default": "",
        "description": "Polymarket US API key id (optional). Alias: POLYMARKET_KEY_ID.",
    },
    "POLY_SECRET_KEY": {
        "type": str,
        "default": "",
        "description": "Polymarket US API secret (optional). Alias: POLYMARKET_SECRET_KEY.",
    },
    "POLY_AUTO_BET_ENABLED": {
        "type": int,
        "default": 0,
        "description": "Enable autonomous Polymarket US orders (0=off, 1=on). Uses same AUTO_BET_* gates as Kalshi.",
    },
    "POLY_MIN_NOTIONAL_USD": {
        "type": float,
        "default": 1.0,
        "description": (
            "Polymarket US: bump contract count so estimated cost (qty × unit price) reaches at least "
            "this many USD, rounded up to whole dollars. Set 0 to disable."
        ),
    },
    "DISCORD_POLY_ID": {
        "type": int,
        "default": 0,
        "description": (
            "Discord channel ID for Polymarket-only alerts. "
            "Use 0 or omit to post Polymarket messages in DISCORD_CHANNEL_ID."
        ),
    },
    "STATE_DIRECTORY": {
        "type": str,
        "default": "",
        "description": (
            "Persistent root for logs/, data/, logs_poly/, data_poly/ (positions, scores, "
            "signal_watch + skip learning in signals.json, ensemble bias). "
            "On Railway mount a volume and set this to its path (e.g. /persist). "
            "Empty = project directory (ephemeral on Railway without a volume)."
        ),
    },
}


def _cast(key: str, raw: str, target_type: type):
    """Cast *raw* string to *target_type*, raising a descriptive error on failure."""
    try:
        return target_type(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"CONFIG ERROR – '{key}' cannot be converted to "
            f"{target_type.__name__!r}: got {raw!r}"
        ) from exc


def truthy_config_int(val) -> bool:
    """True for enabled auto-bet flags (0/1), including optional str forms after Discord toggles."""
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
    """Validate and return the fully-typed CONFIG dictionary."""
    missing = []
    config: dict = {}

    for key, meta in _REQUIRED.items():
        raw = os.environ.get(key, "").strip()
        # KALSHI_PRIVATE_KEY_PATH is optional when the B64 env var is set.
        if not raw and key == "KALSHI_PRIVATE_KEY_PATH" and os.environ.get("KALSHI_PRIVATE_KEY_B64", "").strip():
            config[key] = ""   # placeholder — kalshi_client will use B64 instead
            continue
        if not raw:
            missing.append(
                f"  • {key}  – {meta['description']}"
            )
        else:
            config[key] = _cast(key, raw, meta["type"])

    if missing:
        if os.environ.get("PAVLOV_BYPASS_CONFIG", "0") == "1":
            for k in _REQUIRED:
                if k not in config:
                    config[k] = "" if _REQUIRED[k]["type"] == str else 0
        else:
            raise EnvironmentError(
                "pavlov-weather-bot: the following required environment variables "
                "are missing or empty.\n"
                "Copy .env.example to .env and fill in the values:\n\n"
                + "\n".join(missing)
            )

    # Load optional keys (use default if not set).
    for key, meta in _OPTIONAL.items():
        raw = os.environ.get(key, "").strip()
        if raw:
            config[key] = _cast(key, raw, meta["type"])
        else:
            config[key] = meta["default"]

    # Polymarket US: accept POLYMARKET_* if POLY_* unset (Railway-friendly aliases).
    if not str(config.get("POLY_KEY_ID") or "").strip():
        alt = os.environ.get("POLYMARKET_KEY_ID", "").strip()
        if alt:
            config["POLY_KEY_ID"] = alt
    if not str(config.get("POLY_SECRET_KEY") or "").strip():
        alt = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
        if alt:
            config["POLY_SECRET_KEY"] = alt

    # Railway: attach a volume and Railway sets RAILWAY_VOLUME_MOUNT_PATH; use it
    # as persistent state root when STATE_DIRECTORY is not set explicitly.
    if not str(config.get("STATE_DIRECTORY") or "").strip():
        rvm = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        if rvm:
            config["STATE_DIRECTORY"] = rvm

    return config


# ---------------------------------------------------------------------------
# Public export – import CONFIG from anywhere in the project
# ---------------------------------------------------------------------------
CONFIG: dict = _load_config()
