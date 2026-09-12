"""Recover durable weather paper CLV obligations without replaying a trade.

The complete insert payload is frozen inside the autobet before the ledger write.
Recovery inserts missing rows only; it never resets observed checkpoint evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from backend.trading.weather_experiment import metadata as parse_metadata

_FIXED_FIELDS = (
    "candidate_id", "platform", "market_id", "outcome_id", "side",
    "entry_price", "entry_market_price", "entry_effective_cost",
    "entry_ts", "due_15m", "due_1h", "due_close",
)
_TIME_FIELDS = {"entry_ts", "due_15m", "due_1h", "due_close"}
_PRICE_FIELDS = {"entry_price", "entry_market_price", "entry_effective_cost"}
_BATCH_SIZE = 100


class _MissingPayload(ValueError):
    pass


def _time(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("WEATHER_CLV_AWARE_TIMESTAMP_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _price(value):
    result = float(value)
    if not math.isfinite(result) or not 0 < result < 1:
        raise ValueError("WEATHER_CLV_INVALID_ENTRY_PRICE")
    return result


def _fingerprint(payload):
    body = deepcopy(payload)
    body["metadata"].pop("obligation_fingerprint", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()


def build_weather_clv_payload(bet_id, candidate_id, platform, market_id,
                              entry_time, close_time, entry_market_price,
                              entry_effective_cost, metadata):
    """Freeze the exact obligation before its paper fill enters the ledger."""
    entry, close = _time(entry_time), _time(close_time)
    market, effective = _price(entry_market_price), _price(entry_effective_cost)
    meta = json.loads(json.dumps(metadata, allow_nan=False))
    if (not all(isinstance(value, str) and value for value in (bet_id, candidate_id, market_id))
            or platform not in {"kalshi", "polymarket"} or close <= entry
            or effective < market or not isinstance(meta, dict)
            or not meta.get("experiment_id") or not meta.get("config_hash")
            or meta.get("mode") != "paper" or "clv_obligation" in meta):
        raise ValueError("WEATHER_CLV_INVALID_FILL_PROVENANCE")
    for key, expected in (("autobet_id", bet_id), ("signal_candidate_id", candidate_id)):
        if meta.get(key) is not None and meta[key] != expected:
            raise ValueError("WEATHER_CLV_CONFLICTING_FILL_PROVENANCE")
        meta[key] = expected
    meta.update({"event_start": close.isoformat(), "event_start_utc": close.isoformat(),
                 "close_lead_minutes": 5})
    meta.pop("obligation_fingerprint", None)
    outcome_id = meta.get("outcome_id") or "yes"
    if not isinstance(outcome_id, str):
        raise ValueError("WEATHER_CLV_INVALID_OUTCOME_ID")
    payload = {
        "candidate_id": f"weather-fill:{bet_id}", "platform": platform,
        "market_id": market_id, "outcome_id": outcome_id, "side": "YES",
        "entry_price": market, "entry_market_price": market,
        "entry_effective_cost": effective, "entry_ts": entry.isoformat(),
        "due_15m": (entry + timedelta(minutes=15)).isoformat(),
        "due_1h": (entry + timedelta(hours=1)).isoformat(),
        "due_close": (close - timedelta(minutes=5)).isoformat(),
        "status_15m": "pending", "status_1h": "pending", "status_close": "pending",
        "metadata": meta,
    }
    payload["metadata"]["obligation_fingerprint"] = _fingerprint(payload)
    return payload


def _validated_payload(bet):
    ledger_meta = parse_metadata(bet.get("metadata"))
    payload = ledger_meta.get("clv_obligation")
    if not isinstance(payload, dict):
        raise _MissingPayload("WEATHER_CLV_MISSING_FROZEN_PAYLOAD")
    payload_meta = parse_metadata(payload.get("metadata"))
    if (payload_meta.get("obligation_fingerprint") != _fingerprint(payload)
            or payload.get("candidate_id") != f"weather-fill:{bet.get('id')}"
            or payload.get("platform") != bet.get("venue")
            or payload.get("market_id") != bet.get("market_id")
            or str(bet.get("outcome_name")).lower() != "yes"
            or payload_meta.get("autobet_id") != bet.get("id")
            or payload_meta.get("signal_candidate_id") != ledger_meta.get("candidate_id")
            or any(payload_meta.get(key) != ledger_meta.get(key) for key in ("experiment_id", "config_hash"))
            or _time(payload.get("entry_ts")) != _time(bet.get("created_at"))
            or abs(_price(payload.get("entry_market_price")) - _price(bet.get("market_price"))) > 1e-12
            or abs(_price(payload.get("entry_effective_cost")) - _price(ledger_meta.get("q_exec"))) > 1e-12):
        raise ValueError("WEATHER_CLV_LEDGER_PROVENANCE_MISMATCH")
    shares, stake = float(bet["shares"]), float(bet["stake"])
    if (not math.isfinite(shares) or not math.isfinite(stake) or shares <= 0 or stake <= 0
            or abs(stake - shares * payload["entry_effective_cost"]) > 0.0100001):
        raise ValueError("WEATHER_CLV_LEDGER_COST_MISMATCH")
    rebuilt = build_weather_clv_payload(
        bet["id"], ledger_meta["candidate_id"], bet["venue"], bet["market_id"],
        payload["entry_ts"], payload_meta["event_start_utc"],
        payload["entry_market_price"], payload["entry_effective_cost"], payload_meta,
    )
    if rebuilt != payload:
        raise ValueError("WEATHER_CLV_FROZEN_PAYLOAD_MISMATCH")
    return payload


def _matches(stored, payload):
    try:
        for key in _FIXED_FIELDS:
            left, right = stored.get(key), payload[key]
            if key in _TIME_FIELDS:
                if _time(left) != _time(right):
                    return False
            elif key in _PRICE_FIELDS:
                if abs(_price(left) - _price(right)) > 1e-12:
                    return False
            elif left != right:
                return False
        stored_meta = parse_metadata(stored.get("metadata"))
        # Collector metadata may grow, but the frozen original provenance cannot change.
        return all(stored_meta.get(key) == value for key, value in payload["metadata"].items())
    except (TypeError, ValueError, KeyError, OverflowError):
        return False


def ensure_weather_clv_obligations(db, bets):
    """Replay missing frozen payloads and verify immutable fields in bounded batches."""
    stats = {"checked": 0, "inserted": 0, "existing": 0,
             "missing_payload": 0, "conflicts": 0, "errors": 0}
    payloads, invalid_ids = {}, set()
    for bet in bets:
        meta = parse_metadata(bet.get("metadata"))
        if bet.get("mode") != "paper" or not meta.get("experiment_id"):
            continue
        if bet.get("bet_type") != "weather" and bet.get("sport") != "weather":
            continue
        stats["checked"] += 1
        try:
            payload = _validated_payload(bet)
            key = payload["candidate_id"]
            if key in payloads and payloads[key] != payload:
                invalid_ids.add(key)
                raise ValueError("WEATHER_CLV_CONFLICTING_LEDGER_PAYLOAD")
            payloads[key] = payload
        except _MissingPayload:
            stats["missing_payload"] += 1
            stats["errors"] += 1
        except (TypeError, ValueError, KeyError, OverflowError):
            stats["conflicts"] += 1
            stats["errors"] += 1
    for key in invalid_ids:
        payloads.pop(key, None)
    keys = list(payloads)
    for start in range(0, len(keys), _BATCH_SIZE):
        batch = keys[start:start + _BATCH_SIZE]
        try:
            rows = db.table("clv_obligations").select("*").in_("candidate_id", batch).execute().data or []
            by_id = {row.get("candidate_id"): row for row in rows}
            missing = [key for key in batch if key not in by_id]
            if missing:
                db.table("clv_obligations").upsert(
                    [payloads[key] for key in missing],
                    on_conflict="candidate_id", ignore_duplicates=True,
                ).execute()
                verified = db.table("clv_obligations").select("*").in_("candidate_id", missing).execute().data or []
                by_id.update({row.get("candidate_id"): row for row in verified})
            for key in batch:
                row = by_id.get(key)
                if row is None:
                    stats["errors"] += 1
                elif not _matches(row, payloads[key]):
                    stats["conflicts"] += 1
                    stats["errors"] += 1
                else:
                    stats["inserted" if key in missing else "existing"] += 1
        except Exception:
            # Expose persistence failure without logging credentials or request bodies.
            stats["errors"] += len(batch)
    return stats
