"""
Live-trading toggle — the single source of truth for paper vs live mode.

The dashboard writes the toggle to the ``app_settings`` table (key
``live_trading``); every pipeline entrypoint asks :func:`is_live_mode` instead
of reading the env var directly. Live mode requires ALL of:

  1. The toggle: env ``POLYMARKET_LIVE_ENABLED=true`` OR the DB toggle ON.
  2. On GitHub Actions, the explicit ``ALLOW_LIVE_ON_GITHUB_ACTIONS=true`` opt-in.

Downstream, ``run_autobet`` additionally requires the paper track record to
pass ``assess_live_readiness()`` and the guardian to be clear before any real
order is placed — the toggle only expresses *intent*.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from loguru import logger

from backend.db import get_db

_KEY = "live_trading"


def get_live_toggle(db=None) -> dict:
    """Read the dashboard toggle. Fail-safe: any error reads as disabled."""
    db = db or get_db()
    try:
        res = db.table("app_settings").select("value").eq("key", _KEY).execute()
        if res.data:
            value = res.data[0].get("value") or {}
            if isinstance(value, dict):
                return {"enabled": bool(value.get("enabled")), **value}
    except Exception as exc:
        logger.warning(f"live_toggle read failed (treating as OFF): {exc}")
    return {"enabled": False}


def set_live_toggle(enabled: bool, *, by: str = "dashboard", db=None) -> dict:
    db = db or get_db()
    value = {
        "enabled": bool(enabled),
        "enabled_by": by if enabled else None,
        "enabled_at": datetime.now(timezone.utc).isoformat() if enabled else None,
    }
    db.table("app_settings").upsert(
        {"key": _KEY, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="key",
    ).execute()
    logger.info(f"live_toggle set to {'ON' if enabled else 'OFF'} by {by}")
    return value


def is_live_mode(settings=None, db=None) -> bool:
    """True when the system should *attempt* live trading this run."""
    if settings is None:
        from backend.config import get_settings

        settings = get_settings()

    enabled = bool(getattr(settings, "polymarket_live_enabled", False))
    if not enabled:
        enabled = get_live_toggle(db).get("enabled", False)

    if not enabled:
        return False

    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        if os.environ.get("ALLOW_LIVE_ON_GITHUB_ACTIONS", "").lower() != "true":
            logger.warning(
                "Live toggle is ON but ALLOW_LIVE_ON_GITHUB_ACTIONS is not set — "
                "staying in paper mode. Set the repo variable to promote CI runs."
            )
            return False

    return True
