"""Fetch bounded public archives for offline calibration; never access Supabase."""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from backend.weather_historical_calibration import collect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--days", type=int, default=1, help="Dates per station; increase to scale all 21 stations")
    parser.add_argument("--pilot-days", type=int, default=28)
    parser.add_argument("--request-budget", type=int, default=80)
    parser.add_argument("--output", type=Path, default=Path("reports/weather_historical_calibration"))
    args = parser.parse_args()
    if not (1 <= args.days <= 3650 and 1 <= args.pilot_days <= 3650 and 1 <= args.request_budget <= 500):
        parser.error("Dates must be 1..3650 and request budget 1..500")
    if args.start < date(2026, 4, 3) or args.start + timedelta(days=max(args.days, args.pilot_days)) >= date.today():
        parser.error("Use completed dates from April 3, 2026, within the operational archive")
    report = collect(args.output, args.start, args.days, args.pilot_days, args.request_budget)
    print(json.dumps({key: report[key] for key in ("status", "requests_downloaded", "cache_hits", "accepted_station_date_events", "errors")}))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
