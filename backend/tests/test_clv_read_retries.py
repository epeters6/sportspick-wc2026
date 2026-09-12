"""Transient obligation reads recover without retrying writes or backdating CLV."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
from postgrest.exceptions import APIError

from pavlov.pipeline.clv_updater import (
    _read_obligation_page,
    count_clv_obligations,
    update_clv_obligations,
)


def api_error(code):
    error = APIError({
        "message": "JSON could not be generated",
        "code": str(code),
        "hint": None,
        "details": "Gateway Timeout",
    })
    error.code = code
    return error


def database(*read_results):
    db = MagicMock()
    query = MagicMock()
    for method in ("order", "limit", "or_", "gt"):
        getattr(query, method).return_value = query
    query.execute.side_effect = read_results
    db.table.return_value.select.return_value = query
    return db, query


def pending_row(now):
    return {
        "candidate_id": "c1", "platform": "kalshi",
        "market_id": "KX1", "outcome_id": "yes", "side": "YES",
        "status_15m": "pending", "status_1h": "observed", "status_close": "observed",
        "due_15m": (now - timedelta(minutes=1)).isoformat(),
        "metadata": {},
    }


class TestClvReadRetries(unittest.TestCase):
    def test_transient_errors_retry_only_the_failed_select(self):
        request = httpx.Request("GET", "https://example.test/clv_obligations")
        errors = [
            api_error(504), api_error("504"), api_error("PGRST003"),
            httpx.ReadTimeout("timeout"), httpx.ConnectError("connection lost"),
            httpx.RemoteProtocolError("server disconnected"),
            httpx.HTTPStatusError(
                "unavailable", request=request,
                response=httpx.Response(503, request=request),
            ),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)):
                expected = SimpleNamespace(data=[])
                query = MagicMock()
                query.execute.side_effect = [error, expected]
                with patch("pavlov.pipeline.clv_updater.time.sleep") as sleep:
                    self.assertIs(_read_obligation_page(query), expected)
                self.assertEqual(query.execute.call_count, 2)
                sleep.assert_called_once_with(1.0)

    def test_persistent_transient_failure_propagates_after_three_attempts(self):
        error = api_error("504")
        query = MagicMock()
        query.execute.side_effect = error
        with patch("pavlov.pipeline.clv_updater.time.sleep") as sleep:
            with self.assertRaises(APIError) as raised:
                _read_obligation_page(query)
        self.assertIs(raised.exception, error)
        self.assertEqual(query.execute.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1.0), call(2.0)])

    def test_auth_schema_and_programming_failures_are_not_retried(self):
        for error in (api_error("401"), api_error("42501"), api_error("PGRST204"),
                      ValueError("invalid query"), httpx.LocalProtocolError("bad request")):
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)):
                query = MagicMock()
                query.execute.side_effect = error
                with patch("pavlov.pipeline.clv_updater.time.sleep") as sleep:
                    with self.assertRaises(type(error)):
                        _read_obligation_page(query)
                query.execute.assert_called_once()
                sleep.assert_not_called()

    def test_count_retries_middle_page_without_losing_or_duplicating_rows(self):
        first = {"candidate_id": "a", "status_15m": "pending", "status_close": "pending"}
        second = {"candidate_id": "b", "status_15m": "observed", "status_close": "pending"}
        db, query = database(
            SimpleNamespace(data=[first]), api_error(504),
            SimpleNamespace(data=[second]), SimpleNamespace(data=[]),
        )
        with patch("pavlov.pipeline.clv_updater.time.sleep"):
            counts = count_clv_obligations(db)
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["pending_15m"], 1)
        self.assertEqual(counts["observed_15m"], 1)
        self.assertEqual(counts["pending_close"], 2)
        self.assertEqual(query.execute.call_count, 4)
        self.assertEqual(query.gt.call_args_list, [call("candidate_id", "a"), call("candidate_id", "b")])
        db.table.return_value.update.assert_not_called()

    def test_pending_read_retry_does_not_backdate_a_late_price_receipt(self):
        now = datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)
        row = pending_row(now)
        row["due_15m"] = (now - timedelta(minutes=30) + timedelta(seconds=1)).isoformat()
        receipt = now + timedelta(seconds=2)
        db, query = database(
            api_error(504), SimpleNamespace(data=[row]), SimpleNamespace(data=[]),
        )
        fetch = AsyncMock(return_value=(0.5, receipt, receipt))
        with patch("pavlov.pipeline.clv_updater.time.sleep"):
            stats = asyncio.run(update_clv_obligations(fetch, db=db, now=now))
        self.assertEqual(query.execute.call_count, 3)
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(stats["unavailable"], 1)
        written = db.table.return_value.update.call_args.args[0]
        self.assertEqual(written["obs_15m_ts"], receipt.isoformat())
        self.assertEqual(written["metadata"]["15m_reason"], "OBSERVATION_OVERDUE")
        self.assertNotIn("obs_15m_price", written)

    def test_default_batch_timestamp_is_taken_after_database_retry(self):
        before = datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)
        after = before + timedelta(seconds=2)
        row = pending_row(before)
        row["due_15m"] = None
        db, _ = database(api_error(504), SimpleNamespace(data=[row]), SimpleNamespace(data=[]))
        with patch("pavlov.pipeline.clv_updater.datetime") as clock:
            clock.now.return_value = before
            with patch("pavlov.pipeline.clv_updater.time.sleep",
                       side_effect=lambda _: setattr(clock.now, "return_value", after)):
                asyncio.run(update_clv_obligations(AsyncMock(), db=db))
        written = db.table.return_value.update.call_args.args[0]
        self.assertEqual(written["obs_15m_ts"], after.isoformat())
        self.assertEqual(written["updated_at"], after.isoformat())

    def test_failed_pending_reads_never_fetch_prices_or_write(self):
        db, query = database(api_error(504), api_error(504), api_error(504))
        fetch = AsyncMock()
        with patch("pavlov.pipeline.clv_updater.time.sleep"):
            with self.assertRaises(APIError):
                asyncio.run(update_clv_obligations(fetch, db=db))
        self.assertEqual(query.execute.call_count, 3)
        fetch.assert_not_awaited()
        db.table.return_value.update.assert_not_called()

    def test_update_failure_is_not_retried(self):
        now = datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)
        db, _ = database(SimpleNamespace(data=[pending_row(now)]), SimpleNamespace(data=[]))
        writer = db.table.return_value.update.return_value.eq.return_value.execute
        error = api_error(504)
        writer.side_effect = error
        fetch = AsyncMock(return_value=(0.5, now, now))
        with patch("pavlov.pipeline.clv_updater.time.sleep") as sleep:
            with self.assertRaises(APIError) as raised:
                asyncio.run(update_clv_obligations(fetch, db=db, now=now))
        self.assertIs(raised.exception, error)
        writer.assert_called_once()
        sleep.assert_not_called()
