"""Final-outcome request reuse without changing settlement availability."""
from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.ml.prediction_evaluation import resolve_weather_prediction_backlog


NOW = datetime(2026, 9, 13, 15, tzinfo=timezone.utc)
SOURCE = "weather_forecast03_2026_09_v2"


def _row(row_id, *, venue="kalshi", market_id="market-a", created_at=None):
    return {
        "id": row_id,
        "source": SOURCE,
        "event_key": f"weather:{venue}:KNYC:2026-09-12:high",
        "outcome": market_id,
        "created_at": created_at or row_id,
        "is_correct": True,
        "resolved_at": "2026-09-13T01:00:00+00:00",
        "metadata": {
            "platform": venue,
            "eligible_before_observation_gate": True,
            "snapshot_id": row_id,
        },
    }


def _final(venue="kalshi", market_id="market-a", **extra):
    return {
        "resolved": True,
        "winner": "no",
        "venue": venue,
        "market_id": market_id,
        "settlement_value": 0,
        "rules_primary": "Official station report",
        **extra,
    }


class _Query:
    def __init__(self, db):
        self.db = db
        self.filters = []
        self.payload = None
        self.order_key = None
        self.count = None

    def select(self, _fields):
        return self

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def is_(self, key, value):
        self.filters.append(("is", key, value))
        return self

    def order(self, key):
        self.order_key = key
        return self

    def limit(self, count):
        self.count = count
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def _matches(self, row):
        for operation, key, expected in self.filters:
            if "->>" in key:
                root, nested = key.split("->>")
                actual = (row.get(root) or {}).get(nested)
                if isinstance(actual, bool):
                    actual = str(actual).lower()
            else:
                actual = row.get(key)
            if operation == "is":
                if expected != "null" or actual is not None:
                    return False
            elif actual != expected:
                return False
        return True

    def execute(self):
        rows = [row for row in self.db.rows if self._matches(row)]
        if self.payload is not None:
            for row in rows:
                self.db.write_attempts.append(row["id"])
                if row["id"] in self.db.fail_ids:
                    raise RuntimeError("simulated database write failure")
                if self.db.write_hook:
                    self.db.write_hook(row["id"], self.payload)
                row.update(deepcopy(self.payload))
            return SimpleNamespace(data=deepcopy(rows))
        self.db.reads.append((list(self.filters), self.order_key, self.count))
        if self.order_key:
            rows.sort(key=lambda row: row[self.order_key])
        if self.count is not None:
            rows = rows[:self.count]
        return SimpleNamespace(data=deepcopy(rows))


class _DB:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.reads = []
        self.write_attempts = []
        self.fail_ids = set()
        self.write_hook = None

    def table(self, name):
        if name != "model_predictions":
            raise AssertionError(f"Unexpected table: {name}")
        return _Query(self)


class TestWeatherResolutionCache(unittest.IsolatedAsyncioTestCase):
    async def _resolve(self, db, fetcher, **kwargs):
        return await resolve_weather_prediction_backlog(
            db, resolution_fetcher=fetcher, resolved_at=NOW, source=SOURCE, **kwargs
        )

    async def test_final_reuse_preserves_each_rows_metadata_and_timestamp(self):
        for settled_at in (None, "2026-09-13T13:12:00+00:00"):
            with self.subTest(settled_at=settled_at):
                db = _DB([_row("p1"), _row("p2")])
                calls = []

                async def fetch(venue, market_id):
                    calls.append((venue, market_id))
                    return _final(venue, market_id, settled_at=settled_at)

                summary = await self._resolve(db, fetch)
                self.assertEqual(calls, [("kalshi", "market-a")])
                self.assertEqual(summary["resolution_requests"], 1)
                self.assertEqual(summary["resolved_cache_hits"], 1)
                self.assertEqual(summary["resolved_rows"], 2)
                self.assertEqual(summary["errors"], 0)
                for row in db.rows:
                    self.assertFalse(row["is_correct"])
                    self.assertEqual(row["resolved_at"], settled_at or NOW.isoformat())
                    self.assertEqual(row["metadata"], {
                        "platform": "kalshi",
                        "eligible_before_observation_gate": True,
                        "snapshot_id": row["id"],
                        "meteorological_is_correct": True,
                        "meteorological_resolved_at": "2026-09-13T01:00:00+00:00",
                        "label_source": "venue_official",
                        "official_venue": "kalshi",
                        "official_market_id": "market-a",
                        "official_result": "no",
                        "official_settled_at": settled_at,
                        "official_settlement_value": 0,
                        "official_rules_primary": "Official station report",
                    })

    async def test_cache_key_isolates_both_venue_and_exact_market_id(self):
        db = _DB([
            _row("p1"), _row("p2", venue="polymarket"),
            _row("p3", market_id="market-A"), _row("p4"),
        ])
        calls = []

        def fetch(venue, market_id):
            calls.append((venue, market_id))
            return _final(venue, market_id, winner="yes" if venue == "polymarket" else "no")

        summary = await self._resolve(db, fetch)
        self.assertEqual(calls, [
            ("kalshi", "market-a"), ("polymarket", "market-a"), ("kalshi", "market-A"),
        ])
        self.assertEqual(summary["resolution_requests"], 3)
        self.assertEqual(summary["resolved_cache_hits"], 1)
        self.assertEqual([row["is_correct"] for row in db.rows], [False, True, False, False])

    async def test_pending_or_invalid_result_can_become_final_on_next_row(self):
        for initial in (
            None, {}, _final(resolved=False), _final(resolved=1),
            _final(winner="refund"), _final(market_id="another-market"),
        ):
            with self.subTest(initial=initial):
                db = _DB([_row("p1"), _row("p2"), _row("p3")])
                results = iter([initial, _final()])
                summary = await self._resolve(db, lambda *_: next(results))
                self.assertEqual(summary["unresolved_rows"], 1)
                self.assertEqual(summary["resolved_rows"], 2)
                self.assertEqual(summary["resolution_requests"], 2)
                self.assertEqual(summary["resolved_cache_hits"], 1)
                self.assertNotIn("label_source", db.rows[0]["metadata"])

    async def test_ambiguous_identity_keeps_legacy_grading_but_never_caches(self):
        missing_id = _final()
        del missing_id["market_id"]
        for result in (
            missing_id, _final(market_id=None), _final(market_id=""),
            _final(venue="polymarket"), _final(venue=None),
        ):
            with self.subTest(result=result):
                db = _DB([_row("p1"), _row("p2")])
                summary = await self._resolve(db, lambda *_: deepcopy(result))
                self.assertEqual(summary["resolved_rows"], 2)
                self.assertEqual(summary["resolution_requests"], 2)
                self.assertEqual(summary["resolved_cache_hits"], 0)

    async def test_explicit_id_with_omitted_venue_retains_fetcher_contract(self):
        result = _final()
        del result["venue"]
        db = _DB([_row("p1"), _row("p2")])
        summary = await self._resolve(db, lambda *_: result)
        self.assertEqual(summary["resolution_requests"], 1)
        self.assertEqual(summary["resolved_cache_hits"], 1)
        self.assertEqual(summary["resolved_rows"], 2)

    async def test_fetch_exception_does_not_prevent_a_later_row_fetch(self):
        db = _DB([_row("p1"), _row("p2"), _row("p3")])
        calls = 0

        async def fetch(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated resolver failure")
            return _final()

        summary = await self._resolve(db, fetch)
        self.assertEqual(calls, 2)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["resolved_rows"], 2)
        self.assertEqual(summary["unresolved_rows"], 0)
        self.assertEqual(summary["resolution_requests"], 2)
        self.assertEqual(summary["resolved_cache_hits"], 1)
        self.assertEqual(db.write_attempts, ["p2", "p3"])

    async def test_cache_is_discarded_between_invocations(self):
        calls = 0

        def fetch(*_):
            nonlocal calls
            calls += 1
            return _final(winner="yes" if calls == 1 else "no")

        for expected in (True, False):
            db = _DB([_row("p1"), _row("p2")])
            summary = await self._resolve(db, fetch)
            self.assertEqual(summary["resolution_requests"], 1)
            self.assertEqual(summary["resolved_cache_hits"], 1)
            self.assertEqual([row["is_correct"] for row in db.rows], [expected, expected])
        self.assertEqual(calls, 2)

    async def test_write_failure_preserves_other_rows_and_request_accounting(self):
        db = _DB([_row("p1"), _row("p2")])
        db.fail_ids.add("p1")
        summary = await self._resolve(db, lambda *_: _final())
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["resolved_rows"], 1)
        self.assertEqual(summary["resolution_requests"], 1)
        self.assertEqual(summary["resolved_cache_hits"], 1)
        self.assertEqual(db.write_attempts, ["p1", "p2"])
        self.assertNotIn("label_source", db.rows[0]["metadata"])
        self.assertEqual(db.rows[1]["metadata"]["snapshot_id"], "p2")
        self.assertEqual(db.rows[1]["metadata"]["label_source"], "venue_official")

    async def test_each_use_is_isolated_from_fetcher_and_other_write_mutations(self):
        result = _final(rules_primary={"report": ["original"]})
        db = _DB([_row("p1"), _row("p2"), _row("p3")])
        observed = []

        def mutate_payload(row_id, payload):
            rules = payload["metadata"]["official_rules_primary"]
            observed.append(deepcopy(rules))
            rules["report"][0] = row_id

        db.write_hook = mutate_payload
        summary = await self._resolve(db, lambda *_: result)
        self.assertEqual(summary["resolved_cache_hits"], 2)
        self.assertEqual(observed, [{"report": ["original"]}] * 3)
        self.assertEqual(result["rules_primary"], {"report": ["original"]})

    async def test_query_scope_order_limit_and_invalid_row_handling_are_preserved(self):
        rows = [_row("p3"), _row("p1"), _row("p2"), _row("p0")]
        rows[-1]["source"] = "different-experiment"
        ineligible = _row("p00")
        ineligible["metadata"]["eligible_before_observation_gate"] = False
        labeled = _row("p000")
        labeled["metadata"]["label_source"] = "venue_official"
        db = _DB(rows + [ineligible, labeled])
        summary = await self._resolve(db, lambda *_: _final(), row_limit=2)
        self.assertEqual(summary["candidate_rows"], 2)
        self.assertEqual(db.write_attempts, ["p1", "p2"])
        self.assertEqual(db.reads, [([
            ("eq", "source", SOURCE),
            ("is", "metadata->>label_source", "null"),
            ("eq", "metadata->>eligible_before_observation_gate", "true"),
        ], "created_at", 2)])

        invalid = [_row(""), _row("p2", venue="unknown"), _row("p3", market_id="")]
        summary = await self._resolve(_DB(invalid), lambda *_: self.fail("Invalid row fetched"))
        self.assertEqual(summary["invalid_rows"], 3)
        self.assertEqual(summary["resolution_requests"], 0)
        self.assertEqual(summary["resolved_cache_hits"], 0)

    async def test_empty_backlog_has_zero_observation_counters(self):
        summary = await self._resolve(_DB([]), lambda *_: self.fail("Empty backlog fetched"))
        self.assertEqual(summary, {
            "candidate_rows": 0, "resolved_rows": 0, "unresolved_rows": 0,
            "invalid_rows": 0, "errors": 0, "resolution_requests": 0,
            "resolved_cache_hits": 0,
        })


if __name__ == "__main__":
    unittest.main()
