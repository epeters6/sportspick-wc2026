"""Fetch bounded public archives for offline calibration; never access Supabase."""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from backend.weather_historical_calibration import ReceiptCache, collect, collect_all_stations, collect_label_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--label-years", type=int, nargs="+", help="Separate observation-only annual history")
    parser.add_argument("--days", type=int, default=1, help="Dates per station; increase to scale all 21 stations")
    parser.add_argument("--pilot-days", type=int, default=28)
    parser.add_argument("--request-budget", type=int, default=80)
    parser.add_argument("--output", type=Path, default=Path("reports/weather_historical_calibration"))
    parser.add_argument("--all-stations-batch", action="store_true", help="One 21-location archive request per date")
    parser.add_argument("--cache-dir", type=Path, help="Reuse existing receipt cache without replacing it")
    args = parser.parse_args()
    if not (1 <= args.days <= 3650 and 1 <= args.pilot_days <= 3650 and 1 <= args.request_budget <= 500):
        parser.error("Dates must be 1..3650 and request budget 1..500")
    if args.label_years:
        if args.all_stations_batch or args.start or any(year < 2000 or year >= date.today().year for year in args.label_years):
            parser.error("Label history requires completed years from 2000 onward, without --start or --all-stations-batch")
    else:
        count = args.days if args.all_stations_batch else max(args.days, args.pilot_days)
        if not args.start or args.start < date(2026, 4, 3) or args.start + timedelta(days=count - 1) >= date.today():
            parser.error("Use --start with completed dates from April 3, 2026, within the operational archive")
    cache = ReceiptCache(args.cache_dir or args.output / "receipts", args.request_budget,
                         pace=2.5 if args.all_stations_batch else 1.3)
    if args.label_years:
        report = collect_label_history(args.output, args.label_years, args.request_budget, cache,
                                      progress=lambda row: print(json.dumps(row), flush=True))
    elif args.all_stations_batch:
        report = collect_all_stations(args.output, args.start, args.days, args.request_budget, cache,
                                      progress=lambda row: print(json.dumps(row), flush=True))
    else:
        report = collect(args.output, args.start, args.days, args.pilot_days, args.request_budget, cache)
    print(json.dumps({key: report[key] for key in ("status", "requests_downloaded", "cache_hits", "accepted_station_date_events", "errors")}))
    return 0 if report["status"] in ("complete", "complete_with_gaps") else 1


if __name__ == "__main__":
    raise SystemExit(main())
