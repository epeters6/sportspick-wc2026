"""Run a fixed offline training comparison against saved evidence only."""
import argparse
import json
from pathlib import Path

from backend.weather_training_audit import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of")
    args = parser.parse_args()
    report = run(args.input, args.output, as_of=args.as_of)
    print(json.dumps({key: report[key] for key in ("run_id", "selection", "split", "market_baseline", "ablations")}, indent=2))


if __name__ == "__main__":
    main()
