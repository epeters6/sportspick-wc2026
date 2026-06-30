"""Supabase client singleton with Windows-safe HTTP settings."""
from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from httpx import ConnectError, ReadError, TimeoutException
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

from backend.config import get_settings

_client: Client | None = None
_init_lock = threading.Lock()

T = TypeVar("T")

_TRANSIENT_ERRORS = (ReadError, ConnectError, TimeoutException, OSError)


def _make_client() -> Client:
    s = get_settings()
    url = s.supabase_url
    key = s.supabase_service_role_key
    if not url or not key:
        raise RuntimeError(
            "Supabase credentials missing — set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in GitHub Actions secrets"
        )
    # Custom httpx client is only needed on Windows (HTTP/2 WinError 10035).
    if sys.platform == "win32":
        httpx_client = httpx.Client(
            http2=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        options = SyncClientOptions(httpx_client=httpx_client)
        return create_client(url, key, options=options)
    return create_client(url, key)


def get_db() -> Client:
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:
                _client = _make_client()
    return _client


def db_execute(fn: Callable[[], T], *, retries: int = 3) -> T:
    """Retry transient Supabase/httpx socket errors without closing the shared client."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(0.2 * (2 ** attempt))
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("db_execute failed without exception")
