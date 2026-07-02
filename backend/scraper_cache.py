"""Lightweight DB cache for scraper negative hits (404s, dead URLs)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from backend.db import get_db


def cache_get(key: str) -> dict[str, Any] | None:
    """Return cached value if present and not expired."""
    try:
        db = get_db()
        row = (
            db.table("scraper_cache")
            .select("cache_value, expires_at")
            .eq("cache_key", key)
            .maybe_single()
            .execute()
            .data
        )
        if not row:
            return None
        exp = row.get("expires_at")
        if exp:
            try:
                exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if exp_dt < datetime.now(timezone.utc):
                    return None
            except Exception:
                pass
        return row.get("cache_value") or {}
    except Exception as exc:
        logger.debug(f"scraper_cache get failed ({key}): {exc}")
        return None


def cache_set(
    key: str,
    value: dict[str, Any],
    *,
    ttl_hours: int = 48,
) -> None:
    """Upsert a cache entry with optional TTL."""
    try:
        db = get_db()
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        db.table("scraper_cache").upsert(
            {
                "cache_key": key,
                "cache_value": value,
                "expires_at": expires,
            },
            on_conflict="cache_key",
        ).execute()
    except Exception as exc:
        logger.debug(f"scraper_cache set failed ({key}): {exc}")


def cache_is_negative(key: str) -> bool:
    """True when key is cached as a negative result (e.g. no article)."""
    val = cache_get(key)
    return bool(val and val.get("negative"))
