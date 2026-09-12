"""Run the weather-only paper experiment; isolate settlement from forecast failures.

No exchange order methods or messaging tools are called by this entrypoint.
Run with: python -m scripts.run_weather_cycle --report reports/weather/latest.json
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import importlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from backend.trading.weather_experiment import (
    LATEST_KEY, account_state, fetch_experiment_bets, metadata, register_experiment,
)


def _forecast_window_open(config: dict, now: datetime) -> bool:
    from zoneinfo import ZoneInfo
    from pavlov.pipeline.station_mapper import STATION_MAP, get_tz_for_city, get_station_metadata

    if config.get("require_verified_station"):
        return any(now.astimezone(ZoneInfo(get_station_metadata(station)["timezone"])).hour
                   in config["decision_hours_local"] for station in config["stations"])

    return any(
        row["station"] in config["stations"]
        and now.astimezone(ZoneInfo(get_tz_for_city(city))).hour in config["decision_hours_local"]
        for city, row in STATION_MAP.items()
    )


def _claim_forecast_slot(db, experiment_id: str, run_id: str, now: datetime) -> bool:
    # One forecast attempt per UTC hour across schedulers/retries. A failed
    # attempt stays recorded; it cannot be retried against a more favorable quote.
    key = f"weather_forecast_slot:{experiment_id}:{now.strftime('%Y%m%dT%H')}"
    db.table("app_settings").upsert(
        {"key": key, "value": {"run_id": run_id, "started_at": now.isoformat()}},
        on_conflict="key", ignore_duplicates=True,
    ).execute()
    rows = db.table("app_settings").select("value").eq("key", key).execute().data or []
    return len(rows) == 1 and metadata(rows[0].get("value")).get("run_id") == run_id


async def run_cycle(*, report_path: str | Path = "reports/weather/latest.json", db=None,
                    stage_functions: dict | None = None, now: datetime | None = None,
                    experiment_config: dict | None = None, shared_inputs: dict | None = None,
                    persist_latest: bool = True, include_global_stages: bool = True) -> dict:
    """Persist an honest cycle result even when individual data stages fail."""
    if db is None:
        from backend.db import get_db
        db = get_db()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_id = str(uuid.uuid4())
    report = {"status": "healthy", "mode": "paper", "live_ready": False,
              "timestamp": now.isoformat(), "run_id": run_id, "stages": {}, "venues": {}}
    manifest = None

    async def stage(name, function):
        try:
            value = function()
            if inspect.isawaitable(value):
                value = await value
            degraded = isinstance(value, dict) and (
                bool(value.get("errors")) or bool(value.get("read_errors")) or bool(value.get("venue_errors"))
                or bool(value.get("ensemble_fail")) or bool(value.get("persistence_reject"))
                or bool(value.get("normalization_reject")) or bool(value.get("timing_reject"))
                or any("FEE_SCHEDULE" in reason or "SNAPSHOT" in reason or "LEDGER_WRITE" in reason
                       for reason in value.get("rejection_reasons", {}))
            )
            report["stages"][name] = {"status": "degraded" if degraded else "ok", "result": value}
            if degraded and report["status"] != "failed":
                report["status"] = "degraded"
            return value
        except Exception as exc:
            # Do not copy request bodies or credentials into a public status API.
            report["stages"][name] = {"status": "failed", "error": type(exc).__name__}
            if report["status"] != "failed":
                report["status"] = "degraded"
            return None

    manifest = await stage("experiment", lambda: register_experiment(db, config=experiment_config))
    if manifest is None:
        report["status"] = "failed"
    else:
        report["experiment"] = manifest

    if stage_functions is None:
        # Import each stage inside its error boundary. A broken forecast import
        # must not prevent existing positions from settling or hide the report.
        def invoke(module, name, *args, **kwargs):
            return getattr(importlib.import_module(module), name)(*args, **kwargs)
        stage_functions = {
            "settlement_before": lambda: invoke("backend.trading.weather_settlement", "resolve_weather_autobets"),
            "verification": lambda: invoke("backend.ml.weather_verification", "backfill_actuals", max_rows=100),
            "legacy_official_labels": lambda: invoke("backend.ml.prediction_evaluation", "resolve_weather_prediction_backlog", db, row_limit=150, eligible_only=False),
            "forecast": lambda: invoke("backend.models.weather.sync_weather", "sync_weather_predictions", experiment=manifest, run_id=run_id, shared_inputs=shared_inputs),
            "settlement_after": lambda: invoke("backend.trading.weather_settlement", "resolve_weather_autobets"),
            "forward_official_labels": lambda: invoke("backend.ml.prediction_evaluation", "resolve_weather_prediction_backlog",
                db, row_limit=250, eligible_only=False, source=manifest["config"]["prediction_source"]),
            "forward_evaluation": lambda: invoke("backend.trading.weather_forward_report", "build_forward_report", db, manifest),
        }

    # Outcome processing is independent of experiment registration or forecasting.
    for name in ("settlement_before", "verification", "legacy_official_labels"):
        if include_global_stages and name in stage_functions:
            await stage(name, stage_functions[name])

    if manifest is not None:
        if manifest["config"].get("require_verified_station"):
            from backend.trading.weather_clv_repair import ensure_weather_clv_obligations
            await stage("clv_recovery", lambda: ensure_weather_clv_obligations(
                db, fetch_experiment_bets(db, manifest["id"])))
        window_open = await stage("forecast_window", lambda: _forecast_window_open(manifest["config"], now))
        if (window_open and shared_inputs and shared_inputs.get("forecast_hour")
                and datetime.now(timezone.utc).strftime("%Y%m%dT%H") != shared_inputs["forecast_hour"]):
            window_open = False
            report["status"] = "degraded"
            report["stages"]["forecast_window"] = {"status": "degraded", "reason": "WEATHER_RESEARCH_HOUR_EXPIRED"}
        if window_open:
            claimed = await stage("forecast_slot", lambda: _claim_forecast_slot(db, manifest["id"], run_id, now))
            if claimed and "forecast" in stage_functions:
                await stage("forecast", stage_functions["forecast"])
            elif claimed is False:
                report["stages"]["forecast"] = {"status": "skipped", "reason": "FORECAST_SLOT_ALREADY_ATTEMPTED"}
        elif window_open is False:
            report["stages"]["forecast"] = {"status": "skipped", "reason": report["stages"]["forecast_window"].get("reason", "OUTSIDE_FIXED_LOCAL_WINDOWS")}

    if "settlement_after" in stage_functions:
        await stage("settlement_after", stage_functions["settlement_after"])
    if manifest is not None:
        for name in ("forward_official_labels", "forward_evaluation"):
            if name in stage_functions:
                result = await stage(name, stage_functions[name])
                if name == "forward_evaluation":
                    report["forward_evaluation"] = result
        bets = await stage("accounting", lambda: fetch_experiment_bets(db, manifest["id"]))
        # Keep the public report compact: never publish every raw trade here.
        if bets is not None:
            report["stages"]["accounting"]["result"] = {"rows": len(bets)}
            report["venues"] = {venue: account_state(bets, manifest["config"], venue)
                                for venue in manifest["config"]["venues"]}
            if any(row["quarantined"] for row in report["venues"].values()):
                report["status"] = "degraded"

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        if persist_latest:
            db.table("app_settings").upsert({"key": LATEST_KEY, "value": report,
                                            "updated_at": report["completed_at"]}, on_conflict="key").execute()
    except Exception as exc:
        report["status"] = "failed"
        report["stages"]["persist_report"] = {"status": "failed", "error": type(exc).__name__}
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    temp_path.replace(path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="reports/weather/latest.json")
    parser.add_argument("--suite", action="store_true", help="Run the predefined, independent paper research arms")
    args = parser.parse_args()
    # Defense in depth; forecast pipeline itself is paper-only as well.
    os.environ.update({"LIVE_TRADING_ENABLED": "false", "POLYMARKET_LIVE_ENABLED": "false",
                       "AUTO_BET_ENABLED": "0", "POLY_AUTO_BET_ENABLED": "0",
                       "PAVLOV_BYPASS_CONFIG": "1", "DISCORD_WEBHOOK_URL": ""})
    if args.suite:
        from backend.trading.weather_research import run_research_suite
        report = asyncio.run(run_research_suite(report_path=args.report))
    else:
        report = asyncio.run(run_cycle(report_path=args.report))
    print(json.dumps({"status": report["status"], "mode": "paper", "run_id": report["run_id"],
                      "stages": {key: value["status"] for key, value in report["stages"].items()}}))
    return 1 if report["status"] == "failed" or any(stage["status"] == "failed" for stage in report["stages"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
