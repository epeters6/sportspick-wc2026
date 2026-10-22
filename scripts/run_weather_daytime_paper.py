"""Fit once, initialize once, or collect the separate daytime paper experiment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fit-history", type=Path)
    parser.add_argument("--model", type=Path, default=Path("research/weather_daytime_paper/model.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/weather_daytime_paper"))
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    os.environ.update(LIVE_TRADING_ENABLED="false", POLYMARKET_LIVE_ENABLED="false", AUTO_BET_ENABLED="0",
                      POLY_AUTO_BET_ENABLED="0", PAVLOV_BYPASS_CONFIG="1", DISCORD_WEBHOOK_URL="")
    from backend import weather_daytime_paper as paper
    if args.fit_history:
        if args.initialize:
            parser.error("Fitting and database initialization must be separate")
        model = paper.fit(args.fit_history)
        paper.write_once(args.model, model)
        print(json.dumps({"model_id": model["model_id"], "created_at": model["created_at"],
                          "stations": len(model["parameters"]), "training_last_target": model["training_last_target"]}))
        return 0
    model = json.loads(args.model.read_text(encoding="utf-8"))
    paper.verify_model(model)
    from backend.db import get_db
    store = paper.Store(get_db(), model, args.output)
    if args.initialize:
        store.immutable("model", model["model_id"], model)
        # Ignore-conflict protects an existing book: never replenish/reset it.
        store.db.table("app_settings").upsert({"key": store.key("book"), "value": paper.initial_book(model)},
            on_conflict="key", ignore_duplicates=True).execute()
        book = store.load()
        print(json.dumps({"initialized": True, **paper.summary(book)}))
        return 0
    from backend.weather_daytime_collection import run, StrictPublicFeeds
    report = run(store, StrictPublicFeeds(args.output / "public_cache"))
    print(json.dumps({k: report[k] for k in ("status", "captured", "graded", "decision_vectors", "settled_station_dates", "errors")}))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
