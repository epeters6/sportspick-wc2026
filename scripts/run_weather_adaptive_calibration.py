"""Evaluate rolling station corrections using an existing completed GFS cache."""
import argparse
import json
from pathlib import Path

from backend.weather_adaptive_calibration import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.history, args.output)
    print(json.dumps({"run_id": report["run_id"], "status": report["status"],
                      "coverage": report["coverage"],
                      "scores": {m: v["overall"] for m, v in report["evaluation"].items()}}, indent=2))


if __name__ == "__main__":
    main()
