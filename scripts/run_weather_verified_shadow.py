"""Initialize explicitly or collect the separate verified-run shadow comparison."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reports/weather_verified_shadow"))
    args = parser.parse_args()
    os.environ.update(LIVE_TRADING_ENABLED="false", POLYMARKET_LIVE_ENABLED="false", AUTO_BET_ENABLED="0",
                      POLY_AUTO_BET_ENABLED="0", PAVLOV_BYPASS_CONFIG="1", DISCORD_WEBHOOK_URL="")
    from backend.db import get_db
    from backend import weather_verified_shadow as shadow
    from backend.weather_collection_runtime import CollectionFeeds
    from backend.weather_verified_collection import run
    model = json.loads(Path("research/weather_daytime_paper/model.json").read_text(encoding="utf-8"))
    store = shadow.Store(get_db(), model, args.output)
    if args.initialize:
        print(json.dumps(shadow.summary(store.initialize(datetime.now(timezone.utc)))))
        return 0
    feeds = CollectionFeeds(args.output/"public_cache")
    feeds.deadline = time.monotonic()+18*60
    report = run(store, feeds)
    print(json.dumps({k: report[k] for k in ("status", "captured", "graded", "decision_vectors", "settled_station_dates", "errors")}))
    return int(report["status"] == "degraded")


if __name__ == "__main__":
    raise SystemExit(main())
