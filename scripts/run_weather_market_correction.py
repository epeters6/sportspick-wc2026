"""Fit and screen a conservative market correction using saved decision inputs."""
import argparse
import json
from pathlib import Path

from backend.weather_market_correction import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/weather_market_correction"))
    args = parser.parse_args()
    report = run(args.history, args.output)
    print(json.dumps({k: report[k] for k in ("status", "selected_weight", "selected_ridge", "split", "test")}))


if __name__ == "__main__":
    main()
