"""Versioned weather paper accounts and immutable decision evidence.

Reuse existing Supabase tables: app_settings holds the frozen manifest and
model_predictions stores a separate append-only forecast source. Legacy losses
are never erased or credited to this explicitly new paper experiment.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "backend/models/weather/experiment.json"
LATEST_KEY = "weather_cycle_latest"
IMPLEMENTATION_FILES = (
    "backend/models/weather/sync_weather.py",
    "backend/trading/weather_experiment.py",
    "backend/trading/weather_forward_report.py",
    "backend/trading/weather_research.py",
    "backend/trading/weather_clv_repair.py",
    "backend/trading/settlement_integrity.py",
    "backend/trading/weather_settlement.py",
    "backend/ml/weather_execution_calibration.py",
    "backend/ml/weather_mos.py",
    "backend/ml/weather_verification.py",
    "pavlov/pipeline/probability_model.py",
    "pavlov/pipeline/nowcast_features.py",
    "pavlov/pipeline/market_probability.py",
    "pavlov/pipeline/execution_cost.py",
    "pavlov/pipeline/order_simulator.py",
    "pavlov/pipeline/portfolio_optimizer.py",
    "pavlov/pipeline/fee_model.py",
    "pavlov/pipeline/ensemble_client.py",
    "pavlov/pipeline/settlement_resolver.py",
    "pavlov/pipeline/station_mapper.py",
    "pavlov/pipeline/trade_candidate.py",
    "pavlov/pipeline/clv_updater.py",
    "pavlov/pipeline/clv_tracker.py",
    "scripts/run_clv_scheduler.py",
    "scripts/run_weather_cycle.py",
    "pavlov/config.py",
    "pavlov/pipeline/kalshi_client.py",
    "pavlov/polymarket/poly_client.py",
    "backend/ml/intraday_nowcast.py",
)


def metadata(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
    return {}


def read_config(path: Path = CONFIG_PATH) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("mode") != "paper":
        raise ValueError("WEATHER_EXPERIMENT_PAPER_ONLY")
    if not config.get("id") or not config.get("prediction_source"):
        raise ValueError("WEATHER_EXPERIMENT_ID_REQUIRED")
    if set(config.get("venues", [])) != {"kalshi", "polymarket"}:
        raise ValueError("WEATHER_EXPERIMENT_REQUIRES_BOTH_VENUES")
    if not config.get("stations") or not config.get("metrics") or not set(config["metrics"]) <= {"high", "low"}:
        raise ValueError("INVALID_WEATHER_UNIVERSE")
    hours = config.get("decision_hours_local", [])
    if not hours or any(not isinstance(h, int) or not 0 <= h <= 23 for h in hours):
        raise ValueError("INVALID_WEATHER_DECISION_HOURS")
    for key in ("seed_per_venue", "min_net_edge", "max_event_risk_fraction",
                "max_open_risk_fraction", "daily_loss_fraction", "max_drawdown_fraction"):
        value = float(config[key])
        if not math.isfinite(value) or value <= 0 or (key != "seed_per_venue" and value >= 1):
            raise ValueError(f"INVALID_WEATHER_CONFIG:{key}")
    if config["max_event_risk_fraction"] > config["max_open_risk_fraction"]:
        raise ValueError("EVENT_RISK_EXCEEDS_OPEN_RISK")
    mode = config.get("execution_calibration_mode", "required")
    if mode not in {"required", "observe_only"}:
        raise ValueError("INVALID_WEATHER_CALIBRATION_MODE")
    if mode == "observe_only" and config.get("exploratory") is not True:
        raise ValueError("WEATHER_OBSERVE_ONLY_REQUIRES_EXPLORATORY_PAPER")
    return config


def implementation_hash() -> str:
    digest = hashlib.sha256()
    for name in IMPLEMENTATION_FILES:
        digest.update(name.encode())
        # Normalize checkout line endings so Windows and Linux have one identity.
        digest.update((ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode())
    return digest.hexdigest()


def register_experiment(db, *, config: dict | None = None) -> dict:
    config = config or read_config()
    code_hash = implementation_hash()
    canonical = json.dumps({"config": config, "implementation": code_hash}, sort_keys=True)
    config_hash = hashlib.sha256(canonical.encode()).hexdigest()
    manifest = {"id": config["id"], "config": config, "config_hash": config_hash,
                "implementation_hash": code_hash, "created_at": datetime.now(timezone.utc).isoformat()}
    key = f"weather_experiment:{config['id']}"
    # Never update an existing manifest. Concurrent first runs converge on one row.
    db.table("app_settings").upsert({"key": key, "value": manifest}, on_conflict="key", ignore_duplicates=True).execute()
    rows = db.table("app_settings").select("value").eq("key", key).execute().data or []
    stored = metadata(rows[0].get("value")) if len(rows) == 1 else {}
    if stored.get("config_hash") != config_hash:
        raise ValueError("WEATHER_EXPERIMENT_CHANGED: use a new experiment id; history cannot be retuned")
    return stored


def fetch_experiment_bets(db, experiment_id: str, *, page_size: int = 500) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        query = (db.table("autobets").select("*")
                 .eq("mode", "paper").eq("metadata->>experiment_id", experiment_id)
                 .order("id").limit(page_size))
        if cursor is not None:
            query = query.gt("id", cursor)
        batch = query.execute().data or []
        if not batch:
            return rows
        next_cursor = str(batch[-1]["id"])
        if cursor is not None and next_cursor <= cursor:
            raise ValueError("WEATHER_ACCOUNT_PAGINATION_STALLED")
        rows.extend(batch)
        cursor = next_cursor


def account_state(rows: list[dict], config: dict, venue: str, *, now: datetime | None = None) -> dict:
    from backend.trading.settlement_integrity import verify_weather_autobet, parse_timestamp

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = now.date().isoformat()
    seed = float(config["seed_per_venue"])
    result = {"initial_bankroll": seed, "realized_pnl": 0.0, "reserved": 0.0,
              "settled": 0, "open": 0, "quarantined": 0, "daily_losses": 0.0}
    for row in rows:
        meta = metadata(row.get("metadata"))
        if meta.get("experiment_id") != config["id"] or row.get("mode") != "paper":
            continue
        if row.get("venue") not in config["venues"]:
            result["quarantined"] += 1
            try:
                unknown_stake = float(row["stake"])
                if not math.isfinite(unknown_stake) or unknown_stake <= 0:
                    raise ValueError("invalid unknown-venue stake")
            except (KeyError, TypeError, ValueError):
                unknown_stake = seed
            result["reserved"] += unknown_stake
            continue
        if row.get("venue") != venue:
            continue
        try:
            stake = float(row["stake"])
            if not math.isfinite(stake) or stake <= 0:
                raise ValueError("invalid stake")
        except (KeyError, TypeError, ValueError):
            result["quarantined"] += 1
            continue
        created_at = parse_timestamp(row.get("created_at"))
        resolved_at = parse_timestamp(row.get("resolved_at"))
        if (created_at is None or created_at > now
                or (row.get("status") != "open" and
                    (resolved_at is None or resolved_at > now or resolved_at < created_at))):
            result["reserved"] += stake
            result["quarantined"] += 1
            continue
        if row.get("status") == "open":
            result["reserved"] += stake
            result["open"] += 1
        else:
            check = verify_weather_autobet(row, allow_legacy_paper=False)
            official_source = metadata(meta.get("settlement")).get("source")
            if not check.valid or official_source != f"{venue}_official":
                # Reserve all original cash until exact settlement is verified.
                result["reserved"] += stake
                result["quarantined"] += 1
                continue
            pnl = float(check.expected_pnl)
            if not math.isfinite(pnl):
                result["reserved"] += stake
                result["quarantined"] += 1
                continue
            result["realized_pnl"] += pnl
            result["settled"] += 1
            if resolved_at.date().isoformat() == today:
                result["daily_losses"] += max(0.0, -pnl)
    result["equity"] = seed + result["realized_pnl"]
    result["available_cash"] = max(0.0, result["equity"] - result["reserved"])
    reasons = []
    if result["quarantined"]:
        reasons.append("UNVERIFIED_EXPERIMENT_ACCOUNTING")
    if result["equity"] <= 0 or result["available_cash"] <= 0:
        reasons.append("NON_POSITIVE_AVAILABLE_CASH")
    if result["daily_losses"] >= seed * float(config["daily_loss_fraction"]):
        reasons.append("DAILY_LOSS_LIMIT")
    if result["equity"] <= seed * (1 - float(config["max_drawdown_fraction"])):
        reasons.append("EXPERIMENT_DRAWDOWN_LIMIT")
    result["blocked_reasons"] = reasons
    return result


def entry_budget(account: dict, config: dict) -> float:
    if account["blocked_reasons"]:
        return 0.0
    equity = max(0.0, float(account["equity"]))
    return max(0.0, min(float(account["available_cash"]),
                        equity * float(config["max_event_risk_fraction"]),
                        equity * float(config["max_open_risk_fraction"]) - float(account["reserved"])))


def event_scope_reason(config: dict, *, venue: str, station: str, metric: str,
                       lead_days: int, local_hour: int) -> str | None:
    if venue not in config["venues"] or station not in config["stations"] or metric not in config["metrics"]:
        return "OUTSIDE_WEATHER_EXPERIMENT_UNIVERSE"
    if not 0 <= lead_days <= int(config["max_lead_days"]):
        return "OUTSIDE_WEATHER_EXPERIMENT_HORIZON"
    if local_hour not in config["decision_hours_local"]:
        return "OUTSIDE_WEATHER_DECISION_WINDOW"
    return None


def snapshot_id(experiment_id: str, run_id: str, event_key: str, outcome: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"weather-decision:{experiment_id}:{run_id}:{event_key}:{outcome}"))


def save_prediction_snapshot(db, *, manifest: dict, run_id: str, event_key: str,
                             rows: list[dict], decision_at: str) -> int:
    payload = []
    for row in rows:
        probability = float(row["prob"])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("INVALID_DECISION_PROBABILITY")
        item = {
            "id": snapshot_id(manifest["id"], run_id, event_key, str(row["outcome"])),
            "source": manifest["config"]["prediction_source"], "domain": "weather",
            "event_key": event_key, "outcome": row["outcome"], "prob": probability,
            "market_price": row.get("market_price"), "edge": row.get("edge"),
            "created_at": decision_at, "resolved_at": None, "is_correct": None,
            "metadata": {**metadata(row.get("metadata")), "experiment_id": manifest["id"],
                         "config_hash": manifest["config_hash"], "run_id": run_id,
                         "decision_at": decision_at, "snapshot_kind": "pre_execution_forecast"},
        }
        fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True, allow_nan=False).encode()).hexdigest()
        item["metadata"]["decision_fingerprint"] = fingerprint
        payload.append(item)
    if payload:
        # Retry cannot replace prices, probabilities, or an outcome already graded.
        db.table("model_predictions").upsert(payload, on_conflict="id", ignore_duplicates=True).execute()
        persisted = db.table("model_predictions").select("id,metadata").in_("id", [row["id"] for row in payload]).execute().data or []
        by_id = {str(row["id"]): metadata(row.get("metadata")) for row in persisted}
        if any(by_id.get(row["id"], {}).get("decision_fingerprint") != row["metadata"]["decision_fingerprint"] for row in payload):
            raise ValueError("WEATHER_DECISION_SNAPSHOT_CONFLICT")
    return len(payload)
