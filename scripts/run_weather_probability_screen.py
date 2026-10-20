"""Screen fixed low-temperature probabilities using saved data; no network/DB."""
import argparse
import json
from pathlib import Path

from backend.weather_probability_screen import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--market-history", required=True, type=Path)
    parser.add_argument("--expected-history-hash", required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/weather_probability_screen"))
    args = parser.parse_args()
    result = run(args.history, args.market_history, args.output, expected_history_hash=args.expected_history_hash)
    print(json.dumps({k: result[k] for k in ("status", "station_date_events", "scores", "paired_uncertainty",
                                            "cli_venue_disagreement_events", "quote_only_diagnostics")}))
    return 0 if result["status"] == "retrospective_screen_complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
