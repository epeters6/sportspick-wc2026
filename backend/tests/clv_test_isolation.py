"""Test helpers: block all CLV/Supabase writes from unit tests."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def isolate_clv_db():
    """Patch CLV obligation upsert so unit tests never hit Supabase."""
    with patch(
        "pavlov.pipeline.clv_tracker._upsert_clv_obligation",
        return_value=None,
    ) as upsert:
        yield upsert
