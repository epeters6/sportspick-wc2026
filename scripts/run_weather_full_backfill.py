"""One bounded continuation of the 21-station archive, followed by one final retest."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_archive_retest import run as retest
from backend.weather_decision_archive import ArchiveBudgetCache, collect, make_plan
from backend.weather_historical_calibration import ARCHIVE, RequestBudgetExceeded, write_json
from backend.weather_probability_screen import write_once


class FullBackfillCache(ArchiveBudgetCache):
    """Reserve rolling hourly/daily capacity as well as the existing UTC ledger.

    The first ledger conservatively charges today's previous archive use at
    startup. It retains every reservation, including failed HTTP attempts.
    """
    def __init__(self, *args, hourly_limit=3000, rolling_limit=6000, **kwargs):
        super().__init__(*args, **kwargs)
        self.hourly_limit, self.rolling_limit = hourly_limit, rolling_limit
        self.rolling_ledger = self.path / "full_backfill_rolling_usage.json"
        self.last_http_status = None

    def get(self, url, params):
        identity = digest({"url": url, "params": params})
        cached = any((self.path / (identity + suffix)).exists() for suffix in (".json.gz", ".unavailable.json"))
        if url == ARCHIVE and not cached:
            units = len(str(params["latitude"]).split(","))
            # Let the parent reject exhausted per-run budgets without making a
            # rolling reservation for a request that cannot be attempted.
            if self.downloaded >= self.budget or self.units_used + units > self.location_budget:
                return super().get(url, params)
            lock = self.rolling_ledger.with_suffix(".lock")
            with lock.open("x", encoding="utf-8"):
                pass
            try:
                now = datetime.now(timezone.utc)
                daily = json.loads(self.ledger.read_text()) if self.ledger.exists() else {"version": 1, "days": {}}
                if daily.get("version") != 1 or not isinstance(daily.get("days"), dict):
                    raise ValueError("ARCHIVE_USAGE_LEDGER_INVALID")
                used = daily["days"].get(now.date().isoformat(), 0)
                if not isinstance(used, int) or used < 0:
                    raise ValueError("ARCHIVE_USAGE_LEDGER_INVALID")
                if used + units > self.daily_location_budget:
                    raise RequestBudgetExceeded("ARCHIVE_DAILY_ALLOWANCE_EXHAUSTED")
                if self.rolling_ledger.exists():
                    ledger = json.loads(self.rolling_ledger.read_text())
                else:
                    ledger = {"version": 1, "reservations": [{"at": now.isoformat(), "units": used, "kind": "prior_archive_usage"}] if used else []}
                if ledger.get("version") != 1 or not isinstance(ledger.get("reservations"), list):
                    raise ValueError("ROLLING_ARCHIVE_LEDGER_INVALID")
                for entry in ledger["reservations"]:
                    if not isinstance(entry.get("units"), int) or entry["units"] < 0 or utc(entry["at"]) > now:
                        raise ValueError("ROLLING_ARCHIVE_LEDGER_INVALID")
                def used_since(hours):
                    return sum(e["units"] for e in ledger["reservations"] if utc(e["at"]) > now - timedelta(hours=hours))
                if used_since(1) + units > self.hourly_limit or used_since(24) + units > self.rolling_limit:
                    raise RequestBudgetExceeded("ROLLING_ARCHIVE_ALLOWANCE_EXHAUSTED")
                ledger["reservations"].append({"at": now.isoformat(), "units": units, "request_hash": identity})
                write_json(self.rolling_ledger, ledger)
            finally:
                lock.unlink()
        try:
            return super().get(url, params)
        except requests.HTTPError as error:
            self.last_http_status = error.response.status_code if error.response is not None else None
            raise


def run_batch(archive, cache_dir, market_history, output, *, cache_only=False):
    archive, cache_dir, output = Path(archive), Path(cache_dir), Path(output)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    lock = cache_dir / "weather_full_backfill_worker.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(datetime.now(timezone.utc).isoformat())
    try:
        completion = output / "completion.json"
        if completion.exists():
            saved = json.loads(completion.read_text())
            if saved.get("report_file") != saved["report_hash"] + "_report.json":
                raise ValueError("RETEST_COMPLETION_PATH_INVALID")
            report = json.loads((output / saved["report_file"]).read_text())
            if digest(report) != saved["report_hash"] or report.get("status") != "offline_retest_complete":
                raise ValueError("RETEST_COMPLETION_HASH_MISMATCH")
            return {"state": "complete", "retest": report, "downloads": 0}
        progress_path = output / "backfill_progress.json"
        if progress_path.exists():
            prior = json.loads(progress_path.read_text())
            if prior.get("retry_not_before") and utc(prior["retry_not_before"]) > datetime.now(timezone.utc) and not cache_only:
                return prior
        plan = make_plan(date(2026, 4, 3), 200, [8, 11, 14, 17], 1)
        # October 20 included the earlier 4,153-event GFS download. Preserve
        # that day's smaller allowance; future days leave 4,000 free-tier units
        # for ordinary collection and other usage. Also enforce rolling limits.
        daily = 2000 if datetime.now(timezone.utc).date() <= date(2026, 10, 20) else 6000
        cache = FullBackfillCache(cache_dir, request_budget=0 if cache_only else 100,
                                 location_budget=0 if cache_only else 1000, daily_location_budget=daily)
        latest = archive / "latest.json"
        saved = json.loads(latest.read_text()) if latest.exists() else None
        if saved and saved.get("status") in ("complete", "complete_with_gaps"):
            collected = saved
        else:
            collected = collect(plan, archive, cache, progress=lambda p: print(json.dumps(p), flush=True)
                                if p["requests_available"] % 100 == 0 else None)
        result = retest(archive, market_history, output)
        progress = {"checked_at": datetime.now(timezone.utc).isoformat(), "state": "complete" if result["status"] == "offline_retest_complete" else "collecting",
                    "plan_id": plan["plan_id"], "requests_available": collected["requests_available"],
                    "requests_planned": len(plan["requests"]), "planned_location_units": sum(len(t["stations"]) for t in plan["requests"]),
                    "decision_examples": collected["decision_examples"], "station_dates": collected["station_dates"],
                    "stations": collected["stations"], "dataset_hash": collected["dataset_hash"],
                    "downloads_this_batch": cache.downloaded, "location_units_this_batch": cache.units_used,
                    "daily_location_limit": daily, "rolling_24h_limit": cache.rolling_limit,
                    "http_status": cache.last_http_status, "errors": collected["errors"], "retest_status": result["status"]}
        if result["status"] == "offline_retest_complete":
            report_hash = digest(result)
            write_once(completion, {"report_file": report_hash + "_report.json", "report_hash": report_hash,
                                    "dataset_hash": result["dataset_hash"], "completed_at": progress["checked_at"]})
        elif any(not e.get("resumable", False) and e.get("reason") != "CLI_LABEL_MISSING" for e in collected["errors"]):
            progress["state"] = "blocked_provider_or_evidence"
            progress["retry_not_before"] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        else:
            progress["state"] = "waiting_for_next_budgeted_batch"
        write_json(progress_path, progress)
        return progress
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--market-history", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-only", action="store_true")
    args = parser.parse_args()
    result = run_batch(args.archive, args.cache_dir, args.market_history, args.output, cache_only=args.cache_only)
    print(json.dumps({k: v for k, v in result.items() if k != "retest"}))
    return 1 if result["state"] == "blocked_provider_or_evidence" else 0


if __name__ == "__main__":
    raise SystemExit(main())
