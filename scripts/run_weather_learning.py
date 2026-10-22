"""Train and collect a separate weather research model; never place orders."""
from __future__ import annotations

import argparse
import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/weather_v3"))
    parser.add_argument("--history", type=Path, help="Offline export; requires --no-persist")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--force-train", action="store_true")
    args = parser.parse_args()
    if args.history and not args.no_persist:
        parser.error("Offline history cannot overwrite the production research report")
    os.environ.update(LIVE_TRADING_ENABLED="false", POLYMARKET_LIVE_ENABLED="false", AUTO_BET_ENABLED="0",
                      POLY_AUTO_BET_ENABLED="0", PAVLOV_BYPASS_CONFIG="1", DISCORD_WEBHOOK_URL="")
    from backend.ml.weather_learning import SOURCE, VERSION
    from backend.ml.weather_learning.dataset import TRAINING_SOURCES, utc
    from backend.weather_history_cache import load_cached_history
    from backend.weather_collection_runtime import CollectionFeeds, collect_current, guard_inputs
    from backend.ml.weather_learning.training import load_artifact, train
    db = None
    if not args.no_persist:
        from backend.db import get_db
        db = get_db()
    started = datetime.now(timezone.utc)
    collection_deadline = time.monotonic() + 17 * 60
    report = {"version": VERSION, "started_at": started.isoformat(), "mode": "shadow", "live_ready": False,
              "paper_execution_enabled": False, "stages": {}, "errors": []}
    from backend.weather_stage_report import StageReport
    progress = StageReport(report, args.output, db=db, source=SOURCE)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: progress.interrupt(signum))
    progress.save()
    feeds = CollectionFeeds(args.output / "cache")
    if db is not None:
        from backend.ml.weather_learning.outcomes import label_pending
        with progress.stage("labels"):
            report["stages"]["labels"] = label_pending(db, feeds)
    model_dir = args.output / "model"
    artifact = None
    try:
        artifact = load_artifact(model_dir)
    except (OSError, ValueError, KeyError):
        pass
    needs_train = args.force_train or not artifact or (started - utc(artifact["trained_at"])).total_seconds() >= 86400
    rows = None
    if needs_train:
        with progress.stage("training"):
            if args.history:
                rows = json.loads(args.history.read_text(encoding="utf-8"))
            else:
                rows, report["stages"]["history_sync"] = load_cached_history(db, args.output / "history" / "rows.json.gz", TRAINING_SOURCES)
            training_rows = [r for r in rows if utc(r["created_at"]) >= started - timedelta(days=120)]
            report["stages"]["training"] = train(training_rows, model_dir)
            artifact = load_artifact(model_dir)
    else:
        report["stages"]["training"] = json.loads((model_dir / "training.json").read_text(encoding="utf-8"))
    # Separate shadow policy; frozen v3 probabilities and their hash stay intact.
    guard = None
    if args.collect and artifact and (db is not None or args.history):
        from backend.weather_market_guard import fit, load_model, save_model
        with progress.stage("market_guard_training"):
            guard_dir = args.output / "market_guard"
            if (guard_dir / "latest_model.json").exists():
                guard = load_model(guard_dir)  # Corrupt evidence fails visibly.
            if guard is None or (started - utc(guard["created_at"])).total_seconds() >= 86400:
                if rows is None:
                    if args.history:
                        rows = json.loads(args.history.read_text(encoding="utf-8"))
                    else:
                        rows, report["stages"]["history_sync"] = load_cached_history(db, args.output / "history" / "rows.json.gz", TRAINING_SOURCES)
                candidate_guard = fit(rows, as_of=started)
                save_model(candidate_guard, guard_dir)
                guard = candidate_guard
            report["stages"]["market_guard_training"] = {k: guard[k] for k in (
                "model_id", "created_at", "selected", "weight", "reason", "calibration_station_dates", "calibration_dates")}
            (guard_dir / "artifact_model.json").write_text(json.dumps(guard, indent=2, allow_nan=False), encoding="utf-8")
            from backend.weather_market_guard import SPEC
            from backend.weather_probability_screen import write_once
            write_once(guard_dir / "spec.json", SPEC)
        if report["stage_progress"]["market_guard_training"]["status"] != "completed":
            guard = None
    if args.collect:
        from backend.ml.weather_learning.collection import collect
        with progress.stage("collection"):
            feeds.deadline = collection_deadline
            report["stages"]["collection"] = collect_current(args.output, collector=collect, db=db, artifact=artifact, feeds=feeds)
        if guard is not None:
            from backend.weather_market_guard import predict_forward
            with progress.stage("market_guard_forecasts"):
                current = guard_inputs(args.output, report["stages"].get("collection", {}), started)
                report["stages"]["market_guard_forecasts"] = (
                    {"status": "skipped", "reason": "HOURLY_SLOT_ALREADY_ATTEMPTED", "forecast_vectors": 0}
                    if current is None else predict_forward(current, guard, args.output / "market_guard"))
    if db is not None or rows is not None:
        from backend.ml.weather_learning.evaluation import forward_report
        with progress.stage("forward_evaluation"):
            if rows is None:
                rows, report["stages"]["history_sync"] = load_cached_history(db, args.output / "history" / "rows.json.gz", TRAINING_SOURCES)
            report["stages"]["forward_evaluation"] = forward_report([r for r in rows if utc(r["created_at"]) >= started - timedelta(days=120)])
    progress.save(complete=True)
    print(json.dumps({"status": report["status"], "training": report["stages"].get("training", {}).get("status"),
                      "snapshots": report["stages"].get("collection", {}).get("snapshots", 0), "errors": report["errors"]}))
    return 1 if report["status"] == "degraded" else 0


if __name__ == "__main__":
    raise SystemExit(main())
