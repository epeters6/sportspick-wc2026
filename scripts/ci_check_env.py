"""Validate required GitHub Actions secrets before running sync jobs."""
from __future__ import annotations

import os
import sys


def _check(name: str) -> bool:
    value = os.environ.get(name, "")
    if not value.strip():
        print(
            f"::error::{name} is missing or empty — "
            "add it under Settings → Secrets and variables → Actions"
        )
        return False
    if "\n" in value or "\r" in value:
        print(
            f"::error::{name} contains a newline — "
            "re-paste the secret as a single line with no trailing breaks"
        )
        return False
    return True


def main() -> int:
    ok = _check("SUPABASE_URL") and _check("SUPABASE_SERVICE_ROLE_KEY")
    anon = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    if not anon:
        print("::warning::SUPABASE_ANON_KEY is empty (optional for server-side sync)")
    if ok:
        print("Supabase env OK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
