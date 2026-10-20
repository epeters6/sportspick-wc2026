"""Train and collect a separate weather research model; never place orders."""
from __future__ import annotations

import argparse
import json
import os
import signal
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
    from backend.ml.weather_learning.feeds import PublicFeeds
    from backend.ml.weather_learning.training import load_artifact, train
    db = None
    if not args.no_persist:
        from backend.db import get_db
        db = get_db()
    started = datetime.now(timezone.utc)
    report = {"version": VERSION, "started_at": started.isoformat(), "mode": "shadow", "live_ready": False,
              "paper_execution_enabled": False, "stages": {}, "errors": []}
    from backend.weather_stage_report import StageReport
    progress = StageReport(report, args.output, db=db, source=SOURCE)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: progress.interrupt(signum))
    progress.save()
    feeds = PublicFeeds(args.output / "cache")
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
    if args.collect:
        from backend.ml.weather_learning.collection import collect
        with progress.stage("collection"):
            report["stages"]["collection"] = collect(args.output, db=db, artifact=artifact, feeds=feeds)
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
