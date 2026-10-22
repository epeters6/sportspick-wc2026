"""Use cached GFS/official extrema to evaluate station bias and uncertainty."""
import argparse
import json
from pathlib import Path

from backend.weather_station_distribution import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.history, args.output)
    print(json.dumps({"run_id": report["run_id"], "status": report["status"],
                      "test_station_dates": report["coverage"]["test"]["station_dates"],
                      "test_dates": len(report["coverage"]["test"]["dates"]),
                      "scores": {m: value["overall"] for m, value in report["evaluation"].items()}}, indent=2))


if __name__ == "__main__":
    main()
