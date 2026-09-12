"""Predefined paper research arms with isolated ledgers and shared public inputs."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from backend.trading.weather_experiment import CONFIG_PATH, LATEST_KEY, read_config

CONFIG_FILES = ("baseline_v2.json", "edge03_v2.json", "forecast03_v2.json")


def read_research_configs() -> list[dict]:
    configs = [read_config(CONFIG_PATH.parent / "experiments" / name) for name in CONFIG_FILES]
    for field in ("id", "prediction_source"):
        if len({c[field] for c in configs}) != len(configs):
            raise ValueError("WEATHER_RESEARCH_IDENTITY_COLLISION")
    if any(not c.get("require_verified_station") or c.get("exploratory") is not True for c in configs):
        raise ValueError("WEATHER_RESEARCH_REQUIRES_VERIFIED_EXPLORATORY_PAPER")
    return configs


async def run_research_suite(*, report_path="reports/weather/latest.json", db=None,
                             configs=None, now=None, cycle_runner=None) -> dict:
    from scripts.run_weather_cycle import run_cycle
    if db is None:
        from backend.db import get_db
        db = get_db()
    configs = configs or read_research_configs()
    if not configs or any(c.get("mode") != "paper" for c in configs):
        raise ValueError("WEATHER_RESEARCH_PAPER_ONLY")
    if len({c["id"] for c in configs}) != len(configs):
        raise ValueError("WEATHER_RESEARCH_IDENTITY_COLLISION")
    runner = cycle_runner or run_cycle
    started = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    shared_inputs = {"forecast_hour": started.strftime("%Y%m%dT%H")}
    reports = {}
    path = Path(report_path)
    for index, config in enumerate(configs):
        # Every arm owns its immutable hourly claim and decision timestamps.
        # The same captured discovery/forecast inputs do not imply the same fill.
        try:
            result = await runner(
                db=db, now=started, experiment_config=config, shared_inputs=shared_inputs,
                include_global_stages=index == 0, persist_latest=False,
                report_path=path.parent / (config["id"] + ".json"),
            )
        except Exception as exc:
            result = {"status": "failed", "mode": "paper", "live_ready": False, "run_id": str(uuid.uuid4()),
                      "timestamp": started.isoformat(), "experiment": {"id": config["id"], "config": config},
                      "stages": {"research_arm": {"status": "failed", "error": type(exc).__name__}},
                      "venues": {}, "forward_evaluation": None}
        reports[config["id"]] = result
    primary = dict(reports[configs[0]["id"]])
    primary.update({
        "mode": "paper", "live_ready": False, "research_only": True,
        "active_experiment_ids": list(reports), "experiments": reports,
        "display_scope": "Primary arm only; balances and P&L are never combined across research arms.",
        "retired_experiments": [{
            "id": "weather_forward_2026_09_v1",
            "reason": "Chicago forecast used O'Hare instead of the contracts' Midway station. Original evidence is preserved with this known defect.",
        }],
        "comparison_limitations": [
            "All arms use overlapping markets and outcomes; their samples are correlated.",
            "Changing thresholds was prompted by v1 observations. Evaluate the predefined new arms on future outcomes.",
            "Observe-only calibration is an exploratory paper hypothesis, not an approved execution strategy.",
            "Displayed prices and paper fills do not prove real fill quality or profitability.",
        ],
        "input_capture_id": shared_inputs.get("capture_id"),
        "input_captured_at": shared_inputs.get("captured_at"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    statuses = [r.get("status") for r in reports.values()]
    primary["status"] = "failed" if all(s == "failed" for s in statuses) else (
        "degraded" if any(s != "healthy" for s in statuses) else "healthy")
    # Surface secondary stage failures to the existing runner exit-code contract.
    primary["stages"] = dict(primary["stages"])
    primary["stages"]["research_arms"] = {
        "status": "failed" if any(s == "failed" for s in statuses) else
                  ("degraded" if primary["status"] != "healthy" else "ok"),
        "result": {key: value.get("status") for key, value in reports.items()},
    }
    try:
        db.table("app_settings").upsert({
            "key": LATEST_KEY, "value": primary, "updated_at": primary["completed_at"],
        }, on_conflict="key").execute()
    except Exception as exc:
        primary["status"] = "failed"
        primary["stages"]["persist_report"] = {"status": "failed", "error": type(exc).__name__}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(primary, indent=2, allow_nan=False), encoding="utf-8")
    temp_path.replace(path)
    return primary
