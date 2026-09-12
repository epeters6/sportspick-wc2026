"""Weather CLV recovery is idempotent, provenance checked, and never re-trades."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from backend.trading.weather_clv_repair import build_weather_clv_payload, ensure_weather_clv_obligations

ENTRY = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 9, 13, 3, 59, tzinfo=timezone.utc)


def fixture(number=1):
    meta = {"mode": "paper", "experiment_id": "test-v2", "config_hash": "frozen",
            "candidate_id": f"signal-{number}", "q_exec": 0.52,
            "venue_product": "kalshi", "paper_allow_receipt_timestamp": False}
    bet = {"id": f"bet-{number}", "mode": "paper", "venue": "kalshi",
           "sport": "weather", "market_id": f"market-{number}", "outcome_name": "yes",
           "created_at": ENTRY.isoformat(), "market_price": 0.5, "stake": 1.04, "shares": 2,
           "metadata": meta}
    meta["clv_obligation"] = build_weather_clv_payload(
        bet["id"], meta["candidate_id"], bet["venue"], bet["market_id"],
        ENTRY, CLOSE, 0.5, 0.52, deepcopy(meta))
    return bet


class Query:
    def __init__(self, db):
        self.db, self.ids, self.payload = db, [], None
    def select(self, _):
        return self
    def in_(self, key, values):
        assert key == "candidate_id"
        self.ids = values
        return self
    def upsert(self, payload, *, on_conflict, ignore_duplicates):
        assert on_conflict == "candidate_id"
        assert ignore_duplicates is True
        self.payload = payload
        return self
    def execute(self):
        self.db.calls += 1
        if self.db.fail_read and self.payload is None:
            raise RuntimeError("read unavailable")
        if self.payload is not None:
            self.db.write_calls += 1
            if self.db.fail_write:
                raise RuntimeError("write unavailable")
            for row in self.payload:
                self.db.rows.setdefault(row["candidate_id"], deepcopy(row))
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[deepcopy(self.db.rows[key]) for key in self.ids if key in self.db.rows])


class Database:
    def __init__(self):
        self.rows, self.calls, self.write_calls = {}, 0, 0
        self.fail_read = self.fail_write = False
    def table(self, name):
        assert name == "clv_obligations"
        return Query(self)


class TestWeatherCLVRepair(unittest.TestCase):
    def test_payload_freezes_exact_fill_times_and_provenance(self):
        bet = fixture()
        payload = bet["metadata"]["clv_obligation"]
        self.assertEqual(payload["candidate_id"], "weather-fill:bet-1")
        self.assertEqual(payload["entry_ts"], ENTRY.isoformat())
        self.assertEqual(payload["due_close"], "2026-09-13T03:54:00+00:00")
        self.assertEqual(payload["metadata"]["autobet_id"], bet["id"])
        self.assertEqual(payload["metadata"]["signal_candidate_id"], "signal-1")
        self.assertNotIn("clv_obligation", payload["metadata"])

    def test_missing_obligation_recovers_and_observations_are_never_reset(self):
        db, bet = Database(), fixture()
        first = ensure_weather_clv_obligations(db, [bet])
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["errors"], 0)
        row = db.rows["weather-fill:bet-1"]
        row.update({"status_close": "observed", "obs_close_price": 0.7})
        row["metadata"]["close_receipt_ts"] = "2026-09-13T03:55:00+00:00"
        before = deepcopy(row)
        again = ensure_weather_clv_obligations(db, [bet])
        self.assertEqual(again["existing"], 1)
        self.assertEqual(again["errors"], 0)
        self.assertEqual(db.write_calls, 1)
        self.assertEqual(db.rows["weather-fill:bet-1"], before)

    def test_failed_insert_is_visible_and_later_recovery_uses_original_time(self):
        db, bet = Database(), fixture()
        db.fail_write = True
        failed = ensure_weather_clv_obligations(db, [bet])
        self.assertEqual(failed["errors"], 1)
        self.assertEqual(failed["inserted"], 0)
        db.fail_write = False
        restored = ensure_weather_clv_obligations(db, [bet])
        self.assertEqual(restored["inserted"], 1)
        self.assertEqual(db.rows["weather-fill:bet-1"]["entry_ts"], ENTRY.isoformat())

    def test_missing_payload_never_fabricates_an_obligation(self):
        db, bet = Database(), fixture()
        del bet["metadata"]["clv_obligation"]
        result = ensure_weather_clv_obligations(db, [bet])
        self.assertEqual(result["missing_payload"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertFalse(db.rows)
        self.assertEqual(db.calls, 0)

    def test_ledger_mismatch_or_tampering_is_rejected_before_write(self):
        for field in ("id", "market_id", "venue", "created_at", "market_price", "stake", "candidate", "payload"):
            with self.subTest(field=field):
                db, bet = Database(), fixture()
                if field == "candidate":
                    bet["metadata"]["candidate_id"] = "other-arm-signal"
                elif field == "payload":
                    bet["metadata"]["clv_obligation"]["metadata"]["experiment_id"] = "other-arm"
                elif field in ("market_price", "stake"):
                    bet[field] = 0.9
                elif field == "created_at":
                    bet[field] = "2026-09-12T14:01:00+00:00"
                else:
                    bet[field] = "different"
                result = ensure_weather_clv_obligations(db, [bet])
                self.assertEqual(result["errors"], 1)
                self.assertEqual(result["conflicts"], 1)
                self.assertEqual(db.write_calls, 0)

    def test_persisted_identity_mismatch_is_visible_and_not_overwritten(self):
        db, bet = Database(), fixture()
        row = deepcopy(bet["metadata"]["clv_obligation"])
        row["entry_effective_cost"] = 0.6
        db.rows[row["candidate_id"]] = row
        result = ensure_weather_clv_obligations(db, [bet])
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(db.write_calls, 0)
        self.assertEqual(row["entry_effective_cost"], 0.6)

    def test_every_immutable_obligation_field_is_checked_on_readback(self):
        fields = ("candidate_id", "platform", "market_id", "outcome_id", "side",
                  "entry_price", "entry_market_price", "entry_effective_cost",
                  "entry_ts", "due_15m", "due_1h", "due_close", "metadata")
        for field in fields:
            with self.subTest(field=field):
                db, bet = Database(), fixture()
                key = "weather-fill:bet-1"
                row = deepcopy(bet["metadata"]["clv_obligation"])
                if field == "metadata":
                    row[field]["experiment_id"] = "other-arm"
                elif field in ("entry_price", "entry_market_price", "entry_effective_cost"):
                    row[field] = 0.6
                elif field.endswith("_ts") or field.startswith("due_"):
                    row[field] = "2026-09-12T14:09:00+00:00"
                else:
                    row[field] = "different"
                db.rows[key] = row
                result = ensure_weather_clv_obligations(db, [bet])
                self.assertGreater(result["errors"], 0)
                self.assertEqual(result["inserted"], 0)

    def test_timestamp_serialization_changes_do_not_create_false_conflicts(self):
        db, bet = Database(), fixture()
        row = deepcopy(bet["metadata"]["clv_obligation"])
        row["entry_ts"] = row["entry_ts"].replace("+00:00", "Z")
        db.rows[row["candidate_id"]] = row
        self.assertEqual(ensure_weather_clv_obligations(db, [bet])["errors"], 0)

    def test_live_and_historical_nonexperiment_rows_are_not_replayed(self):
        db, live, legacy = Database(), fixture(), fixture(2)
        live["mode"] = "live"
        del legacy["metadata"]["experiment_id"]
        result = ensure_weather_clv_obligations(db, [live, legacy])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(db.calls, 0)

    def test_recovery_batches_ledger_reads_and_writes(self):
        db = Database()
        bets = [fixture(index) for index in range(205)]
        result = ensure_weather_clv_obligations(db, bets)
        self.assertEqual(result["inserted"], 205)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(db.calls, 9)
        self.assertEqual(db.write_calls, 3)

    def test_read_failure_is_visible_and_does_not_attempt_a_blind_write(self):
        db, bet = Database(), fixture()
        db.fail_read = True
        result = ensure_weather_clv_obligations(db, [bet])
        self.assertEqual(result["errors"], 1)
        self.assertEqual(db.write_calls, 0)

    def test_builder_rejects_missing_timezone_expired_close_and_bad_cost(self):
        for entry, close, market, cost in (
            (ENTRY.replace(tzinfo=None), CLOSE, 0.5, 0.52),
            (ENTRY, ENTRY, 0.5, 0.52), (ENTRY, CLOSE, 0.5, 0.4),
            (ENTRY, CLOSE, 0.5, float("nan")),
        ):
            with self.subTest(entry=entry, close=close, cost=cost):
                with self.assertRaises(ValueError):
                    build_weather_clv_payload("bet", "signal", "kalshi", "market",
                                              entry, close, market, cost,
                                              {"mode": "paper", "experiment_id": "test", "config_hash": "frozen"})


if __name__ == "__main__":
    unittest.main()
