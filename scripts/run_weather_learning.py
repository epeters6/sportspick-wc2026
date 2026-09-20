"""Train and collect a separate weather research model; never place orders."""
from __future__ import annotations

import argparse
import json
import os
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
    from backend.ml.weather_learning.dataset import load_history, utc
    from backend.ml.weather_learning.feeds import PublicFeeds
    from backend.ml.weather_learning.training import load_artifact, train
    db = None
    if not args.no_persist:
        from backend.db import get_db
        db = get_db()
    started = datetime.now(timezone.utc)
    report = {"version": VERSION, "started_at": started.isoformat(), "mode": "shadow", "live_ready": False,
              "paper_execution_enabled": False, "stages": {}, "errors": []}
    feeds = PublicFeeds(args.output / "cache")
    if db is not None:
        from backend.ml.weather_learning.outcomes import label_pending
        try:
            report["stages"]["labels"] = label_pending(db, feeds)
        except Exception as exc:
            report["errors"].append({"stage": "labels", "reason": type(exc).__name__})
    model_dir = args.output / "model"
    artifact = None
    try:
        artifact = load_artifact(model_dir)
    except (OSError, ValueError, KeyError):
        pass
    needs_train = args.force_train or not artifact or (started - utc(artifact["trained_at"])).total_seconds() >= 86400
    rows = None
    if needs_train:
        try:
            rows = json.loads(args.history.read_text(encoding="utf-8")) if args.history else load_history(db, since=(started - timedelta(days=120)).isoformat())
            report["stages"]["training"] = train(rows, model_dir)
            artifact = load_artifact(model_dir)
        except Exception as exc:
            report["errors"].append({"stage": "training", "reason": type(exc).__name__})
    else:
        report["stages"]["training"] = json.loads((model_dir / "training.json").read_text(encoding="utf-8"))
    if args.collect:
        from backend.ml.weather_learning.collection import collect
        try:
            report["stages"]["collection"] = collect(args.output, db=db, artifact=artifact, feeds=feeds)
        except Exception as exc:
            report["errors"].append({"stage": "collection", "reason": type(exc).__name__})
    if db is not None or rows is not None:
        from backend.ml.weather_learning.evaluation import forward_report
        try:
            if rows is None:
                rows = load_history(db, since=(started - timedelta(days=120)).isoformat())
            report["stages"]["forward_evaluation"] = forward_report(rows)
        except Exception as exc:
            report["errors"].append({"stage": "forward_evaluation", "reason": type(exc).__name__})
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "degraded" if report["errors"] or any(v.get("errors") for v in report["stages"].values()) else "healthy"
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "latest.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    if db is not None:
        db.table("app_settings").upsert({"key": SOURCE + "_latest", "value": report, "updated_at": report["completed_at"]}, on_conflict="key").execute()
    print(json.dumps({"status": report["status"], "training": report["stages"].get("training", {}).get("status"),
                      "snapshots": report["stages"].get("collection", {}).get("snapshots", 0), "errors": report["errors"]}))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
