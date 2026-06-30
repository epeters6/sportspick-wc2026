"""Validate required GitHub Actions secrets before running sync jobs."""
from __future__ import annotations

import os
import sys


def _check(name: str, *, looks_like_url: bool = False) -> bool:
    raw = os.environ.get(name, "")
    value = raw.strip()
    if not value:
        print(
            f"::error::{name} is missing or empty — "
            "add it under Settings → Secrets and variables → Actions"
        )
        return False
    # Trailing newlines in pasted secrets are common; config.py strips them at runtime.
    if raw != value or "\n" in raw or "\r" in raw:
        print(
            f"::warning::{name} has leading/trailing whitespace or line breaks — "
            "will be stripped automatically"
        )
    if looks_like_url and not value.startswith(("http://", "https://")):
        print(f"::error::{name} does not look like a URL (expected https://...)")
        return False
    return True


def main() -> int:
    ok = (
        _check("SUPABASE_URL", looks_like_url=True)
        and _check("SUPABASE_SERVICE_ROLE_KEY")
    )
    anon = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    if not anon:
        print("::warning::SUPABASE_ANON_KEY is empty (optional for server-side sync)")
    if ok:
        print("Supabase env OK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
