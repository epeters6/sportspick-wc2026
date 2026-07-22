"""Unit tests for durable CLV obligations updater.

Proves pending → observed (with side-correct fetch args) and pending → unavailable
when overdue with no book. The four live DB stub rows previously marked unavailable
reflect missing books — not successful CLV observations.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


class TestClvObligationsUpdater(unittest.TestCase):
    def test_observes_15m_side_correct_yes_token(self):
        from pavlov.pipeline.clv_updater import update_clv_obligations

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        due_15 = now - timedelta(minutes=20)
        due_1h = now + timedelta(hours=1)
        row = {
            "candidate_id": "c1",
            "market_id": "m1",
            "outcome_id": "tok_yes_abc",
            "side": "YES",
            "status_15m": "pending",
            "status_1h": "pending",
            "status_close": "pending",
            "due_15m": due_15.isoformat(),
            "due_1h": due_1h.isoformat(),
            "due_close": None,
            "metadata": {},
        }

        db = MagicMock()
        select_chain = MagicMock()
        select_chain.or_.return_value.execute.return_value.data = [row]
        db.table.return_value.select.return_value = select_chain
        update_chain = MagicMock()
        db.table.return_value.update.return_value = update_chain
        update_chain.eq.return_value.execute.return_value = MagicMock()

        seen = {}

        async def fetch_ok(mid, oid, side):
            seen["args"] = (mid, oid, side)
            self.assertEqual(mid, "m1")
            self.assertEqual(oid, "tok_yes_abc")
            self.assertEqual(side, "YES")
            # (price, book_ts, received_ts) — obs_ts must use fetch receipt
            return 0.55, now - timedelta(seconds=1), now - timedelta(seconds=2)

        stats = asyncio.run(update_clv_obligations(fetch_ok, db=db, now=now))
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(seen["args"], ("m1", "tok_yes_abc", "YES"))
        patch = db.table.return_value.update.call_args[0][0]
        self.assertEqual(patch["status_15m"], "observed")
        self.assertEqual(patch["obs_15m_price"], 0.55)
        self.assertEqual(
            patch["obs_15m_ts"], (now - timedelta(seconds=2)).isoformat()
        )
        self.assertEqual(
            (patch.get("metadata") or {}).get("15m_receipt_ts"),
            (now - timedelta(seconds=2)).isoformat(),
        )
        # 1h not yet due — must not flip status_1h in this patch
        self.assertNotIn("status_1h", patch)

    def test_close_post_start_unavailable_without_price(self):
        from pavlov.pipeline.clv_updater import update_clv_obligations

        now = datetime(2026, 7, 21, 19, 10, tzinfo=timezone.utc)
        event_start = now - timedelta(minutes=1)
        due_close = event_start - timedelta(minutes=5)
        row = {
            "candidate_id": "c_close",
            "platform": "kalshi",
            "market_id": "KX1",
            "outcome_id": "yes",
            "side": "YES",
            "status_15m": "observed",
            "status_1h": "observed",
            "status_close": "pending",
            "due_15m": due_close.isoformat(),
            "due_1h": due_close.isoformat(),
            "due_close": due_close.isoformat(),
            "metadata": {
                "event_start_utc": event_start.isoformat(),
                "close_lead_minutes": 5,
            },
        }
        db = MagicMock()
        select_chain = MagicMock()
        select_chain.or_.return_value.execute.return_value.data = [row]
        db.table.return_value.select.return_value = select_chain
        update_chain = MagicMock()
        db.table.return_value.update.return_value = update_chain
        update_chain.eq.return_value.execute.return_value = MagicMock()

        async def fetch_ok(mid, oid, side):
            return 0.50, now, now

        stats = asyncio.run(update_clv_obligations(fetch_ok, db=db, now=now))
        self.assertGreaterEqual(stats["unavailable"], 1)
        self.assertEqual(stats["updated"], 0)
        patch = db.table.return_value.update.call_args[0][0]
        self.assertEqual(patch["status_close"], "unavailable")
        self.assertNotIn("obs_close_price", patch)
        self.assertEqual(
            (patch.get("metadata") or {}).get("close_reason"), "POST_START"
        )

    def test_close_rejects_post_start_book_in_window(self):
        from pavlov.pipeline.clv_updater import update_clv_obligations

        now = datetime(2026, 7, 21, 19, 6, tzinfo=timezone.utc)
        event_start = now + timedelta(minutes=2)
        due_close = event_start - timedelta(minutes=5)
        row = {
            "candidate_id": "c_close2",
            "platform": "kalshi",
            "market_id": "KX1",
            "outcome_id": "yes",
            "side": "YES",
            "status_15m": "observed",
            "status_1h": "observed",
            "status_close": "pending",
            "due_15m": due_close.isoformat(),
            "due_1h": due_close.isoformat(),
            "due_close": due_close.isoformat(),
            "metadata": {
                "event_start_utc": event_start.isoformat(),
                "close_lead_minutes": 5,
            },
        }
        db = MagicMock()
        select_chain = MagicMock()
        select_chain.or_.return_value.execute.return_value.data = [row]
        db.table.return_value.select.return_value = select_chain
        update_chain = MagicMock()
        db.table.return_value.update.return_value = update_chain
        update_chain.eq.return_value.execute.return_value = MagicMock()

        post_start_book = event_start + timedelta(seconds=1)

        async def fetch_late_book(mid, oid, side):
            return 0.51, post_start_book, now

        stats = asyncio.run(update_clv_obligations(fetch_late_book, db=db, now=now))
        self.assertEqual(stats["updated"], 0)
        patch = db.table.return_value.update.call_args[0][0]
        self.assertEqual(patch["status_close"], "unavailable")
        self.assertEqual(
            (patch.get("metadata") or {}).get("close_reason"), "POST_START_BOOK"
        )

    def test_polymarket_requires_book_timestamp(self):
        from pavlov.pipeline.clv_updater import update_clv_obligations

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        due_15 = now - timedelta(minutes=20)
        row = {
            "candidate_id": "c_poly",
            "platform": "polymarket",
            "market_id": "pm1",
            "outcome_id": "tok",
            "side": "YES",
            "status_15m": "pending",
            "status_1h": "pending",
            "status_close": "pending",
            "due_15m": due_15.isoformat(),
            "due_1h": due_15.isoformat(),
            "due_close": None,
            "metadata": {},
        }
        db = MagicMock()
        select_chain = MagicMock()
        select_chain.or_.return_value.execute.return_value.data = [row]
        db.table.return_value.select.return_value = select_chain
        update_chain = MagicMock()
        db.table.return_value.update.return_value = update_chain
        update_chain.eq.return_value.execute.return_value = MagicMock()

        async def fetch_receipt_only(mid, oid, side):
            return 0.44, None, now  # no CLOB book_ts

        stats = asyncio.run(update_clv_obligations(fetch_receipt_only, db=db, now=now))
        self.assertEqual(stats["updated"], 0)
        # Still pending within grace — must not observe without book_ts
        self.assertFalse(db.table.return_value.update.called)

    def test_observes_side_correct_no_token(self):
        from pavlov.pipeline.clv_updater import update_clv_obligations

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        due_15 = now - timedelta(minutes=20)
        row = {
            "candidate_id": "c_no",
            "market_id": "m_no",
            "outcome_id": "tok_no_xyz",
            "side": "NO",
            "status_15m": "pending",
            "status_1h": "observed",
            "status_close": "pending",
            "due_15m": due_15.isoformat(),
            "due_1h": due_15.isoformat(),
            "due_close": None,
            "metadata": {},
        }
        db = MagicMock()
        select_chain = MagicMock()
        select_chain.or_.return_value.execute.return_value.data = [row]
        db.table.return_value.select.return_value = select_chain
        update_chain = MagicMock()
        db.table.return_value.update.return_value = update_chain
        update_chain.eq.return_value.execute.return_value = MagicMock()

        async def fetch_no(mid, oid, side):
            self.assertEqual(oid, "tok_no_xyz")
            self.assertEqual(side, "NO")
            return 0.41, now

        stats = asyncio.run(update_clv_obligations(fetch_no, db=db, now=now))
        self.assertEqual(stats["updated"], 1)
        patch = db.table.return_value.update.call_args[0][0]
        self.assertEqual(patch["status_15m"], "observed")
        self.assertEqual(patch["obs_15m_price"], 0.41)

    def test_overdue_without_price_becomes_unavailable(self):
        from pavlov.pipeline.clv_updater import update_clv_obligations

        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        due_15 = now - timedelta(hours=2)
        row = {
            "candidate_id": "c2",
            "market_id": "m1",
            "outcome_id": "tok2",
            "side": "YES",
            "status_15m": "pending",
            "status_1h": "observed",
            "status_close": "pending",
            "due_15m": due_15.isoformat(),
            "due_1h": due_15.isoformat(),
            "due_close": None,
            "metadata": {},
        }
        db = MagicMock()
        select_chain = MagicMock()
        select_chain.or_.return_value.execute.return_value.data = [row]
        db.table.return_value.select.return_value = select_chain
        update_chain = MagicMock()
        db.table.return_value.update.return_value = update_chain
        update_chain.eq.return_value.execute.return_value = MagicMock()

        async def fetch_none(mid, oid, side):
            return None, None

        stats = asyncio.run(update_clv_obligations(fetch_none, db=db, now=now))
        self.assertGreaterEqual(stats["unavailable"], 1)
        patch = db.table.return_value.update.call_args[0][0]
        self.assertEqual(patch["status_15m"], "unavailable")
        self.assertEqual(
            (patch.get("metadata") or {}).get("15m_reason"),
            "OBSERVATION_OVERDUE",
        )

    def test_price_available_but_three_hours_late_is_unavailable(self):
        """A valid current price must not be labeled as a timely 15m observation."""
        from pavlov.pipeline.clv_updater import update_clv_obligations

        now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
        due_15 = now - timedelta(hours=3)  # 3h late vs 30m grace
        row = {
            "candidate_id": "c_late",
            "market_id": "m1",
            "outcome_id": "tok_late",
            "side": "YES",
            "status_15m": "pending",
            "status_1h": "pending",
            "status_close": "pending",
            "due_15m": due_15.isoformat(),
            "due_1h": due_15.isoformat(),
            "due_close": None,
            "metadata": {},
        }
        db = MagicMock()
        select_chain = MagicMock()
        select_chain.or_.return_value.execute.return_value.data = [row]
        db.table.return_value.select.return_value = select_chain
        update_chain = MagicMock()
        db.table.return_value.update.return_value = update_chain
        update_chain.eq.return_value.execute.return_value = MagicMock()

        async def fetch_ok(mid, oid, side):
            return 0.62, now  # price available, but observation is overdue

        stats = asyncio.run(update_clv_obligations(fetch_ok, db=db, now=now))
        self.assertGreaterEqual(stats["unavailable"], 1)
        self.assertEqual(stats["updated"], 0)
        patch = db.table.return_value.update.call_args[0][0]
        self.assertEqual(patch["status_15m"], "unavailable")
        self.assertNotIn("obs_15m_price", patch)
        meta = patch.get("metadata") or {}
        self.assertEqual(meta.get("15m_reason"), "OBSERVATION_OVERDUE")
        self.assertTrue(meta.get("15m_price_available_but_late"))
        self.assertEqual(meta.get("15m_late_price_not_accepted"), 0.62)
        self.assertIn("15m_receipt_ts", meta)
        self.assertGreaterEqual(meta.get("15m_obs_delay_seconds", 0), 3 * 3600 - 1)
