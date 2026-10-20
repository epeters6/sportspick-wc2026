"""Collect a bounded original-run panel at station-local decision times."""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from backend.weather_decision_archive import ArchiveBudgetCache, collect, make_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--days", required=True, type=int)
    parser.add_argument("--hours", type=int, nargs="+", default=[8, 11, 14, 17])
    parser.add_argument("--lead-days", type=int, default=1, choices=[0, 1])
    parser.add_argument("--request-budget", type=int, default=60)
    parser.add_argument("--location-budget", type=int, default=600)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if args.start + timedelta(days=args.days - 1) >= date.today() or not 0 <= args.request_budget <= 300 or not 0 <= args.location_budget <= 2000:
        parser.error("Use completed target dates and budgets of 0..300 requests / 0..2000 locations")
    plan = make_plan(args.start, args.days, args.hours, args.lead_days)
    if args.plan_only:
        from backend.weather_probability_screen import write_once
        write_once(args.output / "plan.json", plan)
        print(json.dumps({"plan_id": plan["plan_id"], "requests": len(plan["requests"]),
                          "location_units": sum(len(r["stations"]) for r in plan["requests"]),
                          "decision_examples": len(plan["decisions"])}))
        return 0
    cache = ArchiveBudgetCache(args.cache_dir, request_budget=args.request_budget, location_budget=args.location_budget)
    report = collect(plan, args.output, cache, progress=lambda p: print(json.dumps(p), flush=True))
    print(json.dumps({k: report[k] for k in ("status", "decision_examples", "station_dates", "stations", "requests_downloaded", "cache_hits", "errors")}))
    return 0 if report["status"] in ("complete", "complete_with_gaps") else 1


if __name__ == "__main__":
    raise SystemExit(main())
