"""Bounded original-run weather panels for specific station-local decisions.

Archive publication times are ASSUMED, never verified retroactively. These rows
are offline weather research, not substitutes for saved pre-outcome market data.
Data: Open-Meteo original runs; IEM NWS CLI; Aviation Weather Center stations.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import requests

from backend.ml.weather_learning.contracts import CLI_CODES, station_metadata
from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_historical_calibration import (
    ARCHIVE, CLIMATE, STATIONS, ReceiptCache, RequestBudgetExceeded, climate_labels,
    finite, split_batch, standard_day, write_json,
)
from backend.weather_probability_screen import write_once

VERSION = "weather_decision_archive_v2"
MODELS = {
    "gfs_global": {"cycle_hours": 6, "assumed_release_lag_hours": 6},
    # Ordinary hourly HRRR runs extend only 18h. Next-day full standard-day
    # targets need the 48h 00/06/12/18Z runs; never splice different runs.
    # https://open-meteo.com/en/docs/gfs-api
    "gfs_hrrr": {"cycle_hours": 6, "assumed_release_lag_hours": 3,
                 "run_policy": "extended_range_00_06_12_18_not_latest_hourly"},
    "ecmwf_ifs025": {"cycle_hours": 6, "assumed_release_lag_hours": 6},
}
VARIABLES = "temperature_2m,dew_point_2m,cloud_cover,wind_speed_10m"


def run_pair(model, decision):
    config = MODELS[model]
    # Conservative nominal schedule only. Real delays can exceed this lag.
    limit = utc(decision) - timedelta(hours=config["assumed_release_lag_hours"])
    cycle = config["cycle_hours"]
    current = limit.replace(hour=limit.hour // cycle * cycle, minute=0, second=0, microsecond=0)
    return current, current - timedelta(hours=cycle)


def make_plan(start, days, hours=(8, 11, 14, 17), lead=1, stations=None):
    if not 1 <= days <= 366 or start < date(2026, 4, 3) or lead not in (0, 1):
        raise ValueError("INVALID_ARCHIVE_RANGE_OR_LEAD")
    stations = sorted(stations or set(CLI_CODES.values()))
    if set(stations) - set(CLI_CODES.values()) or not set(hours) <= {8, 11, 14, 17} or not hours:
        raise ValueError("INVALID_STATIONS_OR_DECISION_HOURS")
    decisions, request_stations = [], defaultdict(set)
    for index in range(days):
        target = start + timedelta(days=index)
        for station in stations:
            for hour in sorted(set(hours)):
                local = datetime.combine(target - timedelta(days=lead), datetime.min.time(),
                                         ZoneInfo(station_metadata(station)["timezone"])).replace(hour=hour)
                runs = {model: [r.strftime("%Y-%m-%dT%H:%M") for r in run_pair(model, local)] for model in MODELS}
                decision = {"station": station, "target_date": target.isoformat(), "decision_at": local.astimezone(timezone.utc).isoformat(),
                            "local_hour": hour, "lead_days": lead, "runs": runs}
                decisions.append(decision)
                for model, pair in runs.items():
                    for run in pair:
                        request_stations[(model, run)].add(station)
    requests = [{"model": model, "run": run, "stations": sorted(stations)}
                for (model, run), stations in sorted(request_stations.items(), key=lambda item: (item[0][1], item[0][0]))]
    body = {"version": VERSION, "start": start.isoformat(), "days": days, "hours": sorted(set(hours)), "lead_days": lead,
            "stations": stations, "models": MODELS, "variables": VARIABLES, "decisions": decisions, "requests": requests,
            "historical_availability_verified": False, "production_training_eligible": False}
    return {**body, "plan_id": digest(body)}


class ArchiveBudgetCache(ReceiptCache):
    """Persist a daily location budget and charge before each uncached request.

    Single-model, four-variable, three-day requests: one budget unit/location.
    This local research allowance is separate from other applications' usage.
    """
    def __init__(self, path, *, request_budget=60, location_budget=600, daily_location_budget=2000, session=None, pace=2.5):
        super().__init__(path, request_budget, session=session, pace=pace)
        self.location_budget, self.daily_location_budget = location_budget, daily_location_budget
        self.units_used = 0
        self.ledger = self.path / "decision_archive_usage.json"

    def get(self, url, params):
        identity = digest({"url": url, "params": params})
        negative_path = self.path / (identity + ".unavailable.json")
        if negative_path.exists():
            negative = json.loads(negative_path.read_text(encoding="utf-8"))
            if (negative.get("url") != url or negative.get("params") != params
                    or negative.get("content_hash") != digest(negative.get("data"))
                    or negative.get("unavailable") is not True or not negative.get("received_at")):
                raise ValueError("ARCHIVE_UNAVAILABLE_RECEIPT_INVALID")
            self.hits += 1
            return negative
        cached = (self.path / (identity + ".json.gz")).exists()
        if url == ARCHIVE and not cached:
            units = len(str(params["latitude"]).split(","))
            lock = self.ledger.with_suffix(".lock")
            # A crashed process leaves its lock for inspection; never remove a
            # pre-existing lock, or bypass a corrupted budget ledger.
            with lock.open("x", encoding="utf-8"):
                pass
            try:
                ledger = json.loads(self.ledger.read_text(encoding="utf-8")) if self.ledger.exists() else {"version": 1, "days": {}}
                if ledger.get("version") != 1 or not isinstance(ledger.get("days"), dict):
                    raise ValueError("ARCHIVE_USAGE_LEDGER_INVALID")
                today = datetime.now(timezone.utc).date().isoformat()
                used = ledger["days"].get(today, 0)
                if not isinstance(used, int) or used < 0:
                    raise ValueError("ARCHIVE_USAGE_LEDGER_INVALID")
                if self.downloaded >= self.budget or self.units_used + units > self.location_budget or used + units > self.daily_location_budget:
                    raise RequestBudgetExceeded("ARCHIVE_LOCATION_OR_REQUEST_BUDGET_EXHAUSTED")
                # Charge failed HTTP attempts too, avoiding restart-based budget resets.
                ledger["days"][today] = used + units
                write_json(self.ledger, ledger)
            finally:
                lock.unlink()
            self.units_used += units
            self.pace = max(self.pace, units * .15)  # below 400 locations/minute
        try:
            return super().get(url, params)
        except requests.HTTPError as error:
            response = error.response
            if response is not None and response.status_code == 400:
                data = response.json()
                if isinstance(data, dict) and data.get("error") is True and str(data.get("reason", "")).startswith("The requested model run is not available."):
                    receipt = {"url": url, "params": params, "data": data, "content_hash": digest(data),
                               "received_at": datetime.now(timezone.utc).isoformat(), "unavailable": True, "http_status": 400}
                    write_once(negative_path, receipt)
                    return receipt
            raise


def parameters(task, locations):
    return {"latitude": ",".join(str(row["lat"]) for row in locations),
            "longitude": ",".join(str(row["lon"]) for row in locations),
            "elevation": ",".join(str(row["elev"]) for row in locations), "models": task["model"], "run": task["run"],
            "hourly": VARIABLES, "forecast_days": 3, "temperature_unit": "fahrenheit",
            "wind_speed_unit": "kmh", "timeformat": "unixtime", "timezone": "GMT"}


def summarize_run(receipt, station, target, run):
    start, end = standard_day(station, date.fromisoformat(target))
    if receipt.get("unavailable"):
        return {"run_initialization_utc": run + ":00+00:00", "forecast_receipt_hash": receipt["content_hash"],
                "retrieved_at": receipt["received_at"], "complete_standard_day": False, "hours_available": 0,
                "standard_day_start": start.isoformat(), "standard_day_end": end.isoformat(),
                "low_f": None, "high_f": None, "reason": "ORIGINAL_RUN_UNAVAILABLE"}
    data, hourly = receipt["data"], receipt["data"]["hourly"]
    expected_units = {"temperature_2m": "°F", "dew_point_2m": "°F", "cloud_cover": "%", "wind_speed_10m": "km/h"}
    if any(data.get("hourly_units", {}).get(k) != v for k, v in expected_units.items()):
        raise ValueError("ARCHIVE_WEATHER_UNIT_MISMATCH")
    stamps = hourly["time"]
    if len(stamps) != len(set(stamps)) or any(not finite(t) for t in stamps):
        raise ValueError("ARCHIVE_TIME_ALIGNMENT_INVALID")
    expected = [int(start.timestamp()) + hour * 3600 for hour in range(24)]
    temp = hourly.get("temperature_2m", [])
    if len(temp) != len(stamps):
        raise ValueError("ARCHIVE_TIME_ALIGNMENT_INVALID")
    lookup = dict(zip(stamps, temp))
    values = [lookup.get(stamp) for stamp in expected]
    complete = all(finite(v) for v in values)
    result = {"run_initialization_utc": run + ":00+00:00", "forecast_receipt_hash": receipt["content_hash"],
              "retrieved_at": receipt["received_at"], "complete_standard_day": complete,
              "hours_available": sum(finite(v) for v in values), "standard_day_start": start.isoformat(), "standard_day_end": end.isoformat(),
              "low_f": min(values) if complete else None, "high_f": max(values) if complete else None,
              "location_index": receipt.get("location_index"), "grid": {k: data.get(k) for k in ("latitude", "longitude", "elevation")}}
    # Missing forecast hours, especially HRRR's shorter runs, stay missing.
    for variable in ("dew_point_2m", "cloud_cover", "wind_speed_10m"):
        series = hourly.get(variable, [])
        if len(series) != len(stamps):
            raise ValueError("ARCHIVE_VARIABLE_ALIGNMENT_INVALID")
        lookup = dict(zip(stamps, series))
        value = [lookup.get(stamp) for stamp in expected]
        result[variable + "_day_mean"] = mean(value) if all(finite(v) for v in value) else None
    return result


def collect(plan, output, cache, *, progress=None):
    if (digest({k: v for k, v in plan.items() if k != "plan_id"}) != plan.get("plan_id")
            or plan.get("version") != VERSION or plan.get("models") != MODELS):
        raise ValueError("ARCHIVE_PLAN_IDENTITY_MISMATCH")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    write_once(output / "plan.json", plan)
    report = {"version": VERSION, "plan_id": plan["plan_id"], "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "running", "mode": "offline_research", "live_ready": False, "paper_execution_enabled": False,
              "historical_public_availability_verified": False, "production_training_eligible": False,
              "requests_planned": len(plan["requests"]), "requests_available": 0, "errors": [],
              "decision_examples_planned": len(plan["decisions"])}
    responses, labels = {}, {}
    try:
        inventory = cache.get(STATIONS, {"ids": ",".join(plan["stations"]), "format": "json"})
        locations = {r["icaoId"]: r for r in inventory["data"]}
        if len(locations) != len(inventory["data"]) or set(plan["stations"]) - set(locations):
            raise ValueError("STATION_INVENTORY_INVALID")
        years = sorted({date.fromisoformat(d["target_date"]).year for d in plan["decisions"]})
        for station in plan["stations"]:
            labels[station] = {}
            for year in years:
                receipt = cache.get(CLIMATE, {"station": station, "year": year})
                labels[station].update({k: (v, receipt) for k, v in climate_labels(receipt, station).items()})
        for task in plan["requests"]:
            locs = [locations[s] for s in task["stations"]]
            receipt = cache.get(ARCHIVE, parameters(task, locs))
            if receipt.get("unavailable"):
                for station in task["stations"]:
                    responses[(task["model"], task["run"], station)] = receipt
            else:
                # A single requested location returns an object, not a one-item list.
                normalized = receipt if isinstance(receipt["data"], list) else {**receipt, "data": [receipt["data"]]}
                for station, response in zip(task["stations"], split_batch(normalized, locs)):
                    responses[(task["model"], task["run"], station)] = response
            report["requests_available"] += 1
            if progress:
                progress({"requests_available": report["requests_available"], "requests_planned": len(plan["requests"]),
                          "downloads": cache.downloaded, "location_units_used": getattr(cache, "units_used", None)})
    except RequestBudgetExceeded as error:
        report["errors"].append({"reason": str(error), "resumable": True})
    except Exception as error:
        report["errors"].append({"reason": str(error) if isinstance(error, ValueError) else type(error).__name__, "resumable": False})
    rows, incomplete = [], 0
    for decision in plan["decisions"]:
        station, target = decision["station"], decision["target_date"]
        if any((model, run, station) not in responses for model, runs in decision["runs"].items() for run in runs):
            continue
        if target not in labels.get(station, {}):
            report["errors"].append({"station": station, "target_date": target, "reason": "CLI_LABEL_MISSING"})
            continue
        try:
            forecasts = {}
            for model, runs in decision["runs"].items():
                current, previous = [summarize_run(responses[(model, run, station)], station, target, run) for run in runs]
                forecasts[model] = {"current": current, "previous": previous,
                    "run_age_hours_at_assumed_decision": (utc(decision["decision_at"]) - utc(current["run_initialization_utc"])).total_seconds() / 3600,
                    "assumed_release_lag_hours": MODELS[model]["assumed_release_lag_hours"],
                    "low_revision_f": current["low_f"] - previous["low_f"] if current["complete_standard_day"] and previous["complete_standard_day"] else None,
                    "high_revision_f": current["high_f"] - previous["high_f"] if current["complete_standard_day"] and previous["complete_standard_day"] else None}
            label, receipt = labels[station][target]
            missing = [model for model, value in forecasts.items() if not value["current"]["complete_standard_day"]]
            incomplete += bool(missing)
            rows.append({**decision, "forecasts": forecasts, "incomplete_models": missing,
                         "observed_high_f": label["high"], "observed_low_f": label["low"],
                         "label_product": label["product"], "label_link": "https://mesonet.agron.iastate.edu" + label["link"],
                         "label_receipt_hash": receipt["content_hash"], "label_received_at": receipt["received_at"],
                         "availability_basis": "nominal_release_lag_assumption_not_historical_receipt",
                         "as_of_eligible": False, "production_training_eligible": False})
        except (ValueError, TypeError, KeyError) as error:
            report["errors"].append({"station": station, "target_date": target,
                                     "reason": str(error) if isinstance(error, ValueError) else "ARCHIVE_RESPONSE_STRUCTURE_INVALID"})
    complete = (report["requests_available"] == report["requests_planned"]
                and all(error["reason"] == "CLI_LABEL_MISSING" for error in report["errors"]))
    report.update(status=("complete_with_gaps" if incomplete or report["errors"] else "complete") if complete else "partial_or_blocked",
                  completed_at=datetime.now(timezone.utc).isoformat(), requests_downloaded=cache.downloaded, cache_hits=cache.hits,
                  location_units_used=getattr(cache, "units_used", None), decision_examples=len(rows),
                  station_dates=len({(r["station"], r["target_date"]) for r in rows}),
                  stations=len({r["station"] for r in rows}), examples_with_incomplete_models=incomplete,
                  examples_with_missing_revision=sum(any(v["low_revision_f"] is None for v in r["forecasts"].values()) for r in rows),
                  complete_current_by_model={m: sum(r["forecasts"][m]["current"]["complete_standard_day"] for r in rows) for m in MODELS},
                  all_three_current_complete=sum(all(v["current"]["complete_standard_day"] for v in r["forecasts"].values()) for r in rows),
                  dataset_hash=digest(rows), limitations=["Actual historical publication time is unverified; init time is not availability.",
                  "Regional model horizons can be shorter than a complete next-day standard window; missing values stay missing.",
                  "Synthetic decision times are a research sampling grid, not actual historical market decisions."])
    report["dataset_file"] = digest(rows) + "_dataset.json"
    report["implementation_hash"] = digest({Path(__file__).name: Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n")})
    write_once(output / report["dataset_file"], rows)
    write_once(output / (digest(report) + "_report.json"), report)
    write_json(output / "latest.json", report)
    return report
