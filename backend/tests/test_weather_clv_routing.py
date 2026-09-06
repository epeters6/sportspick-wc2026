"""Public weather CLV routing, paper timestamp evidence and complete pagination."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pavlov.pipeline.clv_updater import count_clv_obligations, update_clv_obligations
from scripts.run_clv_scheduler import _fetch_executable_price


class CappedDB:
    def __init__(self, rows, cap=2, stall=False):
        self.rows = copy.deepcopy(rows)
        self.cap = cap
        self.stall = stall
        self.reads = 0
        self.writes = []

    def table(self, name):
        assert name == "clv_obligations"
        return Query(self)


class Query:
    def __init__(self, db):
        self.db = db
        self.after = None
        self.pending = False
        self.payload = None
        self.identity = None

    def select(self, columns):
        return self

    def order(self, column):
        assert column == "candidate_id"
        return self

    def limit(self, count):
        return self

    def or_(self, expression):
        self.pending = True
        return self

    def gt(self, column, value):
        assert column == "candidate_id"
        self.after = value
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, column, value):
        assert column == "candidate_id"
        self.identity = value
        return self

    def execute(self):
        if self.payload is not None:
            self.db.writes.append((self.identity, self.payload))
            for row in self.db.rows:
                if row["candidate_id"] == self.identity:
                    row.update(self.payload)
            return SimpleNamespace(data=[self.payload])
        self.db.reads += 1
        rows = sorted(self.db.rows, key=lambda row: row["candidate_id"])
        if self.after is not None and not self.db.stall:
            rows = [row for row in rows if row["candidate_id"] > self.after]
        if self.pending:
            rows = [row for row in rows if any(row.get(f"status_{cp}") == "pending" for cp in ("15m", "1h", "close"))]
        return SimpleNamespace(data=copy.deepcopy(rows[:self.db.cap]))


NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)


def obligation(identity="c1"):
    return {
        "candidate_id": identity, "market_id": "tc-temp-test", "outcome_id": "yes",
        "platform": "polymarket", "side": "YES", "status_15m": "pending",
        "status_1h": "observed", "status_close": "observed",
        "due_15m": (NOW - timedelta(minutes=1)).isoformat(),
        "metadata": {"mode": "paper", "experiment_id": "weather-forward-v1",
                     "venue_product": "polymarket_us", "paper_allow_receipt_timestamp": True},
    }


class WeatherClvRoutingTests(unittest.TestCase):
    def book(self, ticker):
        return {"ticker": ticker, "best_ask": .01, "best_bid": .005,
                "yes_ask_size": 100, "yes_bid_size": 40,
                "received_timestamp": NOW, "orderbook_timestamp": None}

    def test_kalshi_one_cent_and_no_complement_use_unsigned_weather_client(self):
        ticker = "KXHIGHNY-26SEP06-T73"
        router = SimpleNamespace(get_top_of_book=AsyncMock(side_effect=AssertionError("legacy route")))
        with patch("pavlov.pipeline.kalshi_client.get_orderbook_as_parsed", return_value=self.book(ticker)) as fetch:
            yes = asyncio.run(_fetch_executable_price(ticker, "yes", "YES", router))
            no = asyncio.run(_fetch_executable_price(ticker, "no", "NO", router))
        self.assertEqual(yes, (.01, None, NOW))
        self.assertEqual(no, (.995, None, NOW))
        self.assertEqual(fetch.call_count, 2)
        router.get_top_of_book.assert_not_called()

    def test_us_slug_never_reaches_international_clob(self):
        slug = "tc-temp-mdwhigh-2026-09-06-gte75lt76f"
        router = SimpleNamespace(get_top_of_book=AsyncMock(side_effect=AssertionError("CLOB route")))
        with patch("pavlov.polymarket.poly_client.get_orderbook_as_parsed", return_value=self.book(slug)):
            self.assertEqual(asyncio.run(_fetch_executable_price(slug, "yes", "YES", router)), (.01, None, NOW))
            self.assertEqual(asyncio.run(_fetch_executable_price(slug, "no", "NO", router)), (.995, None, NOW))
        router.get_top_of_book.assert_not_called()

    def test_us_client_retains_bid_depth_for_no_executability(self):
        from pavlov.polymarket import poly_client

        raw = {"ticker": "tc-temp-test", "bestBid": {"value": ".2"}, "bestAsk": {"value": ".3"},
               "yes_bid_size": 4, "yes_ask_size": 9, "received_timestamp": NOW}
        with patch.object(poly_client, "get_public_client"), patch.object(poly_client, "_bbo_from_book", return_value=raw):
            book = poly_client.get_orderbook_as_parsed("tc-temp-test")
        self.assertEqual(book["yes_bid_size"], 4)
        self.assertEqual(book["bid_size"], 4)

    def test_missing_depth_mismatched_identity_and_invalid_quote_rejected(self):
        slug = "tc-temp-test"
        for changes in ({"yes_ask_size": 0}, {"ticker": "other"}, {"best_ask": 1.0}, {"best_ask": float("nan")}):
            with self.subTest(changes=changes), patch("pavlov.polymarket.poly_client.get_orderbook_as_parsed", return_value={**self.book(slug), **changes}):
                self.assertIsNone(asyncio.run(_fetch_executable_price(slug, "yes", "YES"))[0])

    def test_legacy_token_routing_preserved(self):
        router = SimpleNamespace(get_top_of_book=AsyncMock(return_value={"best_ask": .4, "book_timestamp": NOW, "received_timestamp": NOW}))
        self.assertEqual(asyncio.run(_fetch_executable_price("legacy-condition", "123456", "NO", router)), (.4, NOW, NOW))
        router.get_top_of_book.assert_awaited_once_with(venue="polymarket", token_id="123456", market_id="legacy-condition")


class WeatherClvEvidenceTests(unittest.TestCase):
    def test_pending_and_counts_paginate_until_empty_under_server_cap(self):
        db = CappedDB([obligation(f"c{i}") for i in range(7)], cap=2)
        self.assertEqual(count_clv_obligations(db)["pending_15m"], 7)
        self.assertEqual(db.reads, 5)

        async def fetch(*args):
            return .3, None, NOW

        stats = asyncio.run(update_clv_obligations(fetch, db=db, now=NOW))
        self.assertEqual(stats["updated"], 7)
        self.assertEqual(len(db.writes), 7)
        self.assertEqual(count_clv_obligations(db)["observed_15m"], 7)
        self.assertEqual(asyncio.run(update_clv_obligations(fetch, db=db, now=NOW))["checked"], 0)

    def test_stalled_cursor_fails_without_partial_writes(self):
        db = CappedDB([obligation("c1"), obligation("c2")], cap=1, stall=True)
        with self.assertRaisesRegex(RuntimeError, "cursor"):
            asyncio.run(update_clv_obligations(AsyncMock(), db=db, now=NOW))
        self.assertEqual(db.writes, [])

    def test_receipt_only_us_is_explicit_paper_evidence(self):
        async def fetch(*args):
            return .3, None, NOW

        db = CappedDB([obligation()])
        self.assertEqual(asyncio.run(update_clv_obligations(fetch, db=db, now=NOW))["updated"], 1)
        payload = db.writes[0][1]
        self.assertEqual(payload["metadata"]["15m_book_ts_source"], "received_timestamp")
        self.assertNotIn("book_ts_15m", payload)
        for change in ({"mode": "live"}, {"venue_product": "international"}, {"experiment_id": None}, {"paper_allow_receipt_timestamp": False}):
            row = obligation()
            row["metadata"].update(change)
            db = CappedDB([row])
            self.assertEqual(asyncio.run(update_clv_obligations(fetch, db=db, now=NOW))["updated"], 0)
            self.assertEqual(db.writes, [])

    def test_fetch_receipt_after_grace_cannot_use_earlier_batch_time(self):
        async def fetch(*args):
            return .3, None, NOW + timedelta(minutes=35)

        db = CappedDB([obligation()])
        stats = asyncio.run(update_clv_obligations(fetch, db=db, now=NOW))
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(db.writes[0][1]["status_15m"], "unavailable")

    def test_nonfinite_late_quote_never_enters_evidence_json(self):
        async def fetch(*args):
            return float("nan"), None, NOW

        row = obligation()
        row["due_15m"] = (NOW - timedelta(hours=2)).isoformat()
        db = CappedDB([row])
        asyncio.run(update_clv_obligations(fetch, db=db, now=NOW))
        evidence = db.writes[0][1]["metadata"]
        self.assertNotIn("15m_late_price_not_accepted", evidence)
        self.assertFalse(evidence["15m_price_available_but_late"])

    def test_close_receipt_after_start_rejected_even_with_earlier_book(self):
        row = obligation()
        row.update(status_15m="observed", status_close="pending", due_close=NOW.isoformat())
        row["metadata"].update(event_start_utc=(NOW + timedelta(minutes=5)).isoformat(), close_lead_minutes=5)

        async def fetch(*args):
            return .3, NOW, NOW + timedelta(minutes=6)

        db = CappedDB([row])
        stats = asyncio.run(update_clv_obligations(fetch, db=db, now=NOW))
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(db.writes[0][1]["metadata"]["close_reason"], "POST_START_BOOK")
