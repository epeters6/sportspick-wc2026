"""
Live-trading toggle — the single source of truth for paper vs live mode.

The dashboard expresses *intent* via ``app_settings`` (key ``live_trading``);
every pipeline entrypoint asks :func:`is_live_mode` instead of reading the env
var directly. Live mode requires ALL of:

  1. The toggle: env ``POLYMARKET_LIVE_ENABLED=true`` OR the DB toggle ON.
  2. On GitHub Actions, the explicit ``ALLOW_LIVE_ON_GITHUB_ACTIONS=true`` opt-in.

Enabling the DB toggle is gated by :func:`request_live_toggle` (admin auth,
guardian health, paper readiness). Writes must use the service-role backend —
RLS no longer permits anon/authenticated INSERT/UPDATE on ``app_settings``.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from backend.db import get_db

_KEY = "live_trading"

_STATUS_OK = 200
_STATUS_BAD_REQUEST = 400
_STATUS_UNAUTHORIZED = 401
_STATUS_FORBIDDEN = 403
_STATUS_CONFLICT = 409


def get_live_toggle(db=None) -> dict:
    """Read the dashboard toggle. Fail-safe: any error reads as disabled."""
    try:
        db = db or get_db()
        res = db.table("app_settings").select("value").eq("key", _KEY).execute()
        if res.data:
            value = res.data[0].get("value") or {}
            if isinstance(value, dict):
                return {"enabled": bool(value.get("enabled")), **value}
    except Exception as exc:
        logger.warning(f"live_toggle read failed (treating as OFF): {exc}")
    return {"enabled": False}


def _persist_toggle(enabled: bool, *, by: str, db) -> dict:
    value = {
        "enabled": bool(enabled),
        "enabled_by": by if enabled else None,
        "enabled_at": datetime.now(timezone.utc).isoformat() if enabled else None,
    }
    db.table("app_settings").upsert(
        {
            "key": _KEY,
            "value": value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="key",
    ).execute()
    logger.info(f"live_toggle set to {'ON' if enabled else 'OFF'} by {by}")
    return value


def set_live_toggle(enabled: bool, *, by: str = "dashboard", db=None) -> dict:
    """Low-level writer that will NOT enable live trading.

    Enabling must go through :func:`request_live_toggle` (auth + readiness).
    Disabling remains allowed for emergency shutoff via service-role callers.
    """
    if enabled:
        raise PermissionError(
            "set_live_toggle cannot enable live trading; use request_live_toggle"
        )
    db = db or get_db()
    return _persist_toggle(False, by=by, db=db)


def _bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header or not str(authorization_header).strip():
        return None
    raw = str(authorization_header).strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip() or None
    return raw


def _admin_allowlist() -> list[str]:
    raw = os.environ.get("LIVE_TRADING_ADMIN_ALLOWLIST", "") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Validate admin JWT when possible; never log token contents."""
    try:
        import jwt
    except ImportError:
        jwt = None  # type: ignore

    secret = (os.environ.get("SUPABASE_JWT_SECRET") or "").strip()
    if jwt is not None and secret:
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"require": ["sub"]},
            )
        except Exception as exc:
            logger.warning(f"live_toggle JWT verify failed: {exc}")
            return None

    try:
        client = get_db()
        resp = client.auth.get_user(token)
        user = getattr(resp, "user", None)
        if user is None:
            return None
        return {
            "sub": getattr(user, "id", None),
            "email": getattr(user, "email", None),
            "role": getattr(user, "role", None),
        }
    except Exception as exc:
        logger.warning(f"live_toggle auth.get_user failed: {exc}")
        return None


def _authorize_admin(
    authorization_header: str | None,
) -> tuple[bool, str, str, int]:
    """Return (ok, actor, reason, http_status)."""
    token = _bearer_token(authorization_header)
    if not token:
        return False, "anonymous", "Authorization Bearer token required", _STATUS_UNAUTHORIZED

    admin_token = (os.environ.get("LIVE_TRADING_ADMIN_TOKEN") or "").strip()
    allowlist = _admin_allowlist()

    if not admin_token and not allowlist:
        return (
            False,
            "unknown",
            "LIVE_TRADING_ADMIN_ALLOWLIST or LIVE_TRADING_ADMIN_TOKEN not configured",
            _STATUS_FORBIDDEN,
        )

    if admin_token and secrets.compare_digest(token, admin_token):
        return True, "admin_token", "admin token accepted", _STATUS_OK

    if not allowlist:
        return False, "unknown", "Bearer token is not an authorized admin", _STATUS_FORBIDDEN

    claims = _decode_jwt_claims(token)
    if not claims:
        return False, "unknown", "Invalid or unverifiable admin JWT", _STATUS_UNAUTHORIZED

    allow_set = {a.lower() for a in allowlist}
    candidates = [
        str(claims.get("email") or "").strip(),
        str(claims.get("sub") or "").strip(),
        str(claims.get("user_id") or "").strip(),
        str(claims.get("id") or "").strip(),
    ]
    for candidate in candidates:
        if candidate and candidate.lower() in allow_set:
            actor = claims.get("email") or claims.get("sub") or candidate
            return True, str(actor), "allowlist JWT accepted", _STATUS_OK

    return False, "unknown", "JWT subject not in LIVE_TRADING_ADMIN_ALLOWLIST", _STATUS_FORBIDDEN


def _guardian_halted(db=None) -> tuple[bool, dict[str, Any]]:
    """Read durable Guardian halt from Supabase. Fail closed on any read error."""
    try:
        client = db or get_db()
        res = (
            client.table("app_settings")
            .select("value")
            .eq("key", "guardian_halt")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            # No durable halt recorded — treat as clear (local file is not authoritative).
            return False, {"halted": False, "reasons": [], "updated_at": None, "source": "supabase"}
        value = rows[0].get("value") or {}
        if not isinstance(value, dict):
            return True, {
                "halted": True,
                "reasons": ["guardian_halt value malformed"],
                "source": "supabase",
            }
        halted = bool(value.get("halted"))
        return halted, {**value, "source": "supabase"}
    except Exception as exc:
        logger.warning(f"live_toggle guardian halt read failed (rejecting enable): {exc}")
        return True, {
            "halted": True,
            "reasons": [f"guardian halt unreadable (fail closed): {exc}"],
            "source": "fail_closed",
        }


def _github_live_blocked() -> str | None:
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return None
    if os.environ.get("ALLOW_LIVE_ON_GITHUB_ACTIONS", "").lower() == "true":
        return None
    return "ALLOW_LIVE_ON_GITHUB_ACTIONS is not set — refusing live enable on GitHub Actions"


def _write_audit(
    *,
    db,
    previous_value: dict,
    requested_value: dict,
    actor: str,
    readiness: dict | None,
    reason: str,
    allowed: bool,
) -> None:
    try:
        db.table("live_toggle_audit").insert(
            {
                "previous_value": previous_value,
                "requested_value": requested_value,
                "actor": actor,
                "readiness": readiness,
                "reason": reason,
                "allowed": allowed,
            }
        ).execute()
    except Exception as exc:
        logger.warning(f"live_toggle audit write failed: {exc}")


def request_live_toggle(
    enabled: bool,
    *,
    actor: str,
    authorization_header: str | None = None,
    db=None,
) -> dict[str, Any]:
    """Authorize and (if allowed) apply a live-trading toggle change.

    Always attempts an audit row. Enabling requires admin auth, clear guardian,
    GitHub live opt-in when on Actions, and ``assess_live_readiness()``.
    Defaults to disabled when Supabase is unavailable.
    """
    requested_value = {
        "enabled": bool(enabled),
        "requested_by": actor,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    previous_value: dict = {"enabled": False}
    readiness: dict | None = None

    try:
        db = db or get_db()
        previous_value = get_live_toggle(db)
    except Exception as exc:
        reason = f"Supabase unavailable — live toggle remains OFF ({exc})"
        logger.warning(reason)
        return {
            "allowed": False,
            "toggle": {"enabled": False},
            "reason": reason,
            "status": _STATUS_CONFLICT,
            "http_status": _STATUS_CONFLICT,
            "readiness": None,
            "actor": actor,
        }

    ok, resolved_actor, auth_reason, auth_status = _authorize_admin(authorization_header)
    effective_actor = resolved_actor if ok else (actor or resolved_actor)

    if not ok:
        _write_audit(
            db=db,
            previous_value=previous_value,
            requested_value=requested_value,
            actor=effective_actor,
            readiness=None,
            reason=auth_reason,
            allowed=False,
        )
        return {
            "allowed": False,
            "toggle": previous_value,
            "reason": auth_reason,
            "status": auth_status,
            "http_status": auth_status,
            "readiness": None,
            "actor": effective_actor,
        }

    if not enabled:
        try:
            toggle = _persist_toggle(False, by=effective_actor, db=db)
        except Exception as exc:
            reason = f"Failed to disable live toggle: {exc}"
            _write_audit(
                db=db,
                previous_value=previous_value,
                requested_value=requested_value,
                actor=effective_actor,
                readiness=None,
                reason=reason,
                allowed=False,
            )
            return {
                "allowed": False,
                "toggle": previous_value,
                "reason": reason,
                "status": _STATUS_CONFLICT,
                "http_status": _STATUS_CONFLICT,
                "readiness": None,
                "actor": effective_actor,
            }
        _write_audit(
            db=db,
            previous_value=previous_value,
            requested_value=requested_value,
            actor=effective_actor,
            readiness=None,
            reason="disabled",
            allowed=True,
        )
        return {
            "allowed": True,
            "toggle": toggle,
            "reason": "disabled",
            "status": _STATUS_OK,
            "http_status": _STATUS_OK,
            "readiness": None,
            "actor": effective_actor,
        }

    gh_block = _github_live_blocked()
    if gh_block:
        _write_audit(
            db=db,
            previous_value=previous_value,
            requested_value=requested_value,
            actor=effective_actor,
            readiness=None,
            reason=gh_block,
            allowed=False,
        )
        return {
            "allowed": False,
            "toggle": previous_value,
            "reason": gh_block,
            "status": _STATUS_CONFLICT,
            "http_status": _STATUS_CONFLICT,
            "readiness": None,
            "actor": effective_actor,
        }

    halted, guardian_state = _guardian_halted(db)
    if halted:
        reasons = guardian_state.get("reasons") or ["Guardian circuit breaker tripped"]
        reason = f"Guardian halted: {'; '.join(str(r) for r in reasons)}"
        _write_audit(
            db=db,
            previous_value=previous_value,
            requested_value=requested_value,
            actor=effective_actor,
            readiness={"guardian": guardian_state},
            reason=reason,
            allowed=False,
        )
        return {
            "allowed": False,
            "toggle": previous_value,
            "reason": reason,
            "status": _STATUS_CONFLICT,
            "http_status": _STATUS_CONFLICT,
            "readiness": {"guardian": guardian_state},
            "actor": effective_actor,
        }

    try:
        from backend.trading.autobet_learning import assess_live_readiness

        readiness = assess_live_readiness(db)
    except Exception as exc:
        readiness = {"live_ready": False, "message": f"readiness check failed: {exc}"}

    if not readiness.get("live_ready"):
        reason = readiness.get("message") or "Live readiness checks failed"
        _write_audit(
            db=db,
            previous_value=previous_value,
            requested_value=requested_value,
            actor=effective_actor,
            readiness=readiness,
            reason=reason,
            allowed=False,
        )
        return {
            "allowed": False,
            "toggle": previous_value,
            "reason": reason,
            "status": _STATUS_BAD_REQUEST,
            "http_status": _STATUS_BAD_REQUEST,
            "readiness": readiness,
            "actor": effective_actor,
        }

    try:
        toggle = _persist_toggle(True, by=effective_actor, db=db)
    except Exception as exc:
        reason = f"Failed to enable live toggle (left OFF): {exc}"
        logger.error(reason)
        _write_audit(
            db=db,
            previous_value=previous_value,
            requested_value=requested_value,
            actor=effective_actor,
            readiness=readiness,
            reason=reason,
            allowed=False,
        )
        return {
            "allowed": False,
            "toggle": {"enabled": False},
            "reason": reason,
            "status": _STATUS_CONFLICT,
            "http_status": _STATUS_CONFLICT,
            "readiness": readiness,
            "actor": effective_actor,
        }

    _write_audit(
        db=db,
        previous_value=previous_value,
        requested_value=requested_value,
        actor=effective_actor,
        readiness=readiness,
        reason="enabled",
        allowed=True,
    )
    return {
        "allowed": True,
        "toggle": toggle,
        "reason": "enabled",
        "status": _STATUS_OK,
        "http_status": _STATUS_OK,
        "readiness": readiness,
        "actor": effective_actor,
    }


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
