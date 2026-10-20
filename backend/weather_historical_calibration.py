"""Offline physical-weather calibration. No database, market prices or execution.

NWS CLI labels are midnight-to-midnight local STANDARD time. Archive run
initialization is not historical public availability; these records are research
only and are never eligible for as-of trading tests or production training.
"""
from __future__ import annotations

import gzip
import json
import math
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import requests

from backend.ml.weather_learning.contracts import CLI_CODES, station_metadata
from backend.ml.weather_learning.dataset import digest

ARCHIVE = "https://single-runs-api.open-meteo.com/v1/forecast"
CLIMATE = "https://mesonet.agron.iastate.edu/json/cli.py"
STATIONS = "https://aviationweather.gov/api/data/stationinfo"


class RequestBudgetExceeded(RuntimeError):
    pass


class ReceiptCache:
    """Bounded, resumable requests; corrupt/mismatched receipts fail closed."""

    def __init__(self, path, budget=80, session=None, pace=1.3):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.budget, self.session, self.pace = budget, session or requests.Session(), pace
        self.downloaded = self.hits = 0
        self.last_request = 0.0

    def get(self, url, params):
        identity = {"url": url, "params": params}
        path = self.path / (digest(identity) + ".json.gz")
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                receipt = json.load(stream)
            if (receipt.get("url") != url or receipt.get("params") != params
                    or digest(receipt["data"]) != receipt.get("content_hash")
                    or not receipt.get("received_at")):
                raise ValueError("HISTORICAL_RECEIPT_INVALID")
            self.hits += 1
            return receipt
        if self.downloaded >= self.budget:
            raise RequestBudgetExceeded("REQUEST_BUDGET_EXHAUSTED_RESUME_WITH_SAME_CACHE")
        time.sleep(max(0, self.pace - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.downloaded += 1
        response = self.session.get(url, params=params, timeout=30,
                                    headers={"User-Agent": "weather-historical-calibration/1"})
        response.raise_for_status()  # No automatic retries or rate-limit loops.
        data = response.json()
        receipt = {**identity, "received_at": datetime.now(timezone.utc).isoformat(),
                   "content_hash": digest(data), "data": data}
        temporary = path.with_suffix(".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            json.dump(receipt, stream, allow_nan=False)
        temporary.replace(path)
        return receipt


def standard_day(station, target):
    local = ZoneInfo(station_metadata(station)["timezone"])
    offset = datetime(target.year, 1, 1, tzinfo=local).utcoffset()
    start = datetime.combine(target, day_time(), timezone(offset)).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def climate_labels(receipt, station):
    labels = {}
    for row in receipt["data"]["results"]:
        if row.get("station") != station:
            raise ValueError("CLIMATE_STATION_MISMATCH")
        target = date.fromisoformat(row["valid"])
        if not row.get("product") or not row.get("link"):
            continue
        if not all(finite(row.get(metric)) for metric in ("high", "low")):
            continue
        if row["low"] > row["high"]:
            raise ValueError("CLIMATE_EXTREMA_INVALID")
        if target.isoformat() in labels:
            raise ValueError("DUPLICATE_CLIMATE_DATE")
        labels[target.isoformat()] = row
    return labels


def make_example(station, target, run, forecast, label, label_receipt):
    """Complete 24-hour windows only; hourly extrema remain an approximation."""
    start, end = standard_day(station, target)
    if datetime.fromisoformat(run).replace(tzinfo=timezone.utc) >= start:
        raise ValueError("RUN_MUST_PRECEDE_TARGET_DAY")
    data = forecast["data"]
    if data.get("hourly_units", {}).get("temperature_2m") != "°F":
        raise ValueError("FORECAST_UNIT_MISMATCH")
    hourly = data["hourly"]
    times, temperatures = hourly["time"], hourly["temperature_2m"]
    if len(times) != len(temperatures) or len(set(times)) != len(times):
        raise ValueError("FORECAST_TIME_ALIGNMENT_INVALID")
    expected = [int(start.timestamp()) + hour * 3600 for hour in range(24)]
    by_time = dict(zip(times, temperatures))
    if not all(stamp in by_time and finite(by_time[stamp]) for stamp in expected):
        raise ValueError("INCOMPLETE_STANDARD_DAY")
    values = [by_time[stamp] for stamp in expected]
    return {"station": station, "target_date": target.isoformat(), "model": "gfs_global",
            "run_initialization_utc": run + "+00:00", "standard_day_start": start.isoformat(),
            "standard_day_end": end.isoformat(), "forecast_high_f": max(values),
            "forecast_low_f": min(values), "observed_high_f": label["high"],
            "observed_low_f": label["low"], "label_product": label["product"],
            "label_link": "https://mesonet.agron.iastate.edu" + label["link"],
            "label_receipt_hash": label_receipt["content_hash"],
            "forecast_receipt_hash": forecast["content_hash"],
            "retrieved_at": forecast["received_at"],
            "forecast_grid": {key: data.get(key) for key in ("latitude", "longitude", "elevation")},
            "historical_availability": "unverified", "as_of_eligible": False,
            "production_training_eligible": False, "label_snapshot_is_final_as_retrieved": True}


def evaluate(rows, test_start_date=None):
    """Date-grouped chronological split; high/low are not independent events."""
    dates = sorted({row["target_date"] for row in rows})
    if len(dates) < 14:
        return {"status": "insufficient_dates", "station_date_events": len(rows)}
    split = (sum(day < test_start_date for day in dates) if test_start_date
             else max(7, int(len(dates) * .75)))
    if split < 7 or split == len(dates):
        return {"status": "insufficient_dates", "station_date_events": len(rows)}
    training_dates = set(dates[:split])
    train = [row for row in rows if row["target_date"] in training_dates]
    test = [row for row in rows if row["target_date"] not in training_dates]
    results = {}
    for metric in ("high", "low"):
        bias = mean(row[f"observed_{metric}_f"] - row[f"forecast_{metric}_f"] for row in train)
        results[metric] = {"training_bias_f": bias,
            "raw_test_mae_f": mean(abs(row[f"observed_{metric}_f"] - row[f"forecast_{metric}_f"]) for row in test),
            "corrected_test_mae_f": mean(abs(row[f"observed_{metric}_f"] - (row[f"forecast_{metric}_f"] + bias)) for row in test)}
    return {"status": "exploratory_offline_only", "train_dates": dates[:split], "test_dates": dates[split:],
            "train_station_date_events": len(train), "test_station_date_events": len(test),
            "scores": results, "profitability_evaluated": False, "deployment_authorized": False}


def collect(output, start, days=1, pilot_days=28, budget=80, cache=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cache = cache or ReceiptCache(output / "receipts", budget)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": "offline_research",
              "live_ready": False, "paper_execution_enabled": False,
              "scope": "all_21_registry_stations", "pilot_station": "KPHL",
              "historical_public_availability_verified": False, "stations": {}, "errors": [],
              "limitations": ["Hourly extrema approximate continuous daily extrema.",
                  "Final CLI snapshots are not historical label-publication receipts.",
                  "Run initialization is not public availability; no as-of or production eligibility.",
                  "Physical temperature errors do not establish a trading advantage."]}
    rows = []
    try:
        inventory = cache.get(STATIONS, {"ids": ",".join(sorted(CLI_CODES.values())), "format": "json"})
        coordinates = {row["icaoId"]: row for row in inventory["data"]}
        for station in ["KPHL"] + sorted(set(CLI_CODES.values()) - {"KPHL"}):
            count = max(days, pilot_days) if station == "KPHL" else days
            dates = [start + timedelta(days=i) for i in range(count)]
            summary = {"city": station_metadata(station)["city"], "requested_dates": count,
                       "accepted_station_dates": 0, "label_dates": 0, "evaluation": None}
            report["stations"][station] = summary
            location = coordinates.get(station)
            if location is None:
                report["errors"].append({"station": station, "reason": "STATION_COORDINATES_MISSING"})
                continue
            summary["station_coordinates"] = {key: location.get(key) for key in ("lat", "lon", "elev")}
            labels = {}
            for year in sorted({day.year for day in dates}):
                receipt = cache.get(CLIMATE, {"station": station, "year": year})
                labels.update({key: (value, receipt) for key, value in climate_labels(receipt, station).items()})
            summary["label_dates"] = sum(day.isoformat() in labels for day in dates)
            station_rows = []
            for target in dates:
                if target.isoformat() not in labels:
                    report["errors"].append({"station": station, "date": target.isoformat(), "reason": "CLI_LABEL_MISSING"})
                    continue
                run = (target - timedelta(days=1)).isoformat() + "T00:00"
                forecast = cache.get(ARCHIVE, {"latitude": location["lat"], "longitude": location["lon"],
                    "elevation": location["elev"], "models": "gfs_global", "run": run,
                    "hourly": "temperature_2m", "forecast_days": 3,
                    "temperature_unit": "fahrenheit", "timeformat": "unixtime", "timezone": "GMT"})
                label, receipt = labels[target.isoformat()]
                example = make_example(station, target, run, forecast, label, receipt)
                station_rows.append(example)
                rows.append(example)
                summary["accepted_station_dates"] += 1
            summary["evaluation"] = evaluate(station_rows)
    except RequestBudgetExceeded:
        report["errors"].append({"reason": "REQUEST_BUDGET_EXHAUSTED_RESUME_WITH_SAME_CACHE"})
    except Exception as error:
        # Persist progress and safe failure identity; do not overwrite/delete bad receipts.
        report["errors"].append({"reason": str(error) if isinstance(error, ValueError) else type(error).__name__})
    report.update(completed_at=datetime.now(timezone.utc).isoformat(),
                  requests_downloaded=cache.downloaded, cache_hits=cache.hits,
                  accepted_station_date_events=len(rows), dataset_hash=digest(rows),
                  status="complete" if not report["errors"] else "partial_or_blocked")
    for name, data in (("dataset.json", rows), ("latest.json", report)):
        path = output / name
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    return report


def batch_parameters(locations, target):
    return {"latitude": ",".join(str(row["lat"]) for row in locations),
            "longitude": ",".join(str(row["lon"]) for row in locations),
            "elevation": ",".join(str(row["elev"]) for row in locations),
            "models": "gfs_global", "run": (target - timedelta(days=1)).isoformat() + "T00:00",
            "hourly": "temperature_2m", "forecast_days": 3,
            "temperature_unit": "fahrenheit", "timeformat": "unixtime", "timezone": "GMT"}


def split_batch(receipt, locations):
    """Retain the parent receipt; verify ordered location IDs and grid proximity."""
    data = receipt["data"]
    if not isinstance(data, list) or len(data) != len(locations):
        raise ValueError("ARCHIVE_BATCH_LENGTH_MISMATCH")
    results = []
    for index, (response, location) in enumerate(zip(data, locations)):
        # Open-Meteo omits location_id for index zero.
        if response.get("location_id", 0) != index:
            raise ValueError("ARCHIVE_BATCH_LOCATION_ID_MISMATCH")
        if (not finite(response.get("latitude")) or not finite(response.get("longitude"))
                or abs(response["latitude"] - location["lat"]) > .5
                or abs(response["longitude"] - location["lon"]) > .5
                or response.get("elevation") != location["elev"]
                or response.get("utc_offset_seconds") != 0):
            raise ValueError("ARCHIVE_BATCH_LOCATION_MISMATCH")
        results.append({**receipt, "data": response, "location_index": index})
    return results


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def collect_label_history(output, years, budget=80, cache=None, progress=None):
    """Older official outcomes for seasonal baselines, never fabricated forecasts."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cache = cache or ReceiptCache(output / "receipts", budget)
    rows, coverage, errors = [], {}, []
    try:
        for station in sorted(set(CLI_CODES.values())):
            coverage[station] = {}
            for year in sorted(set(years)):
                receipt = cache.get(CLIMATE, {"station": station, "year": year})
                labels = climate_labels(receipt, station)
                count = 0
                for target, label in sorted(labels.items()):
                    if date.fromisoformat(target).year != year:
                        raise ValueError("CLIMATE_YEAR_MISMATCH")
                    rows.append({"station": station, "target_date": target, "observed_high_f": label["high"],
                        "observed_low_f": label["low"], "label_product": label["product"],
                        "label_link": "https://mesonet.agron.iastate.edu" + label["link"],
                        "label_receipt_hash": receipt["content_hash"], "retrieved_at": receipt["received_at"],
                        "role": "observations_only", "forecast_available": False,
                        "production_training_eligible": False, "as_of_eligible": False})
                    count += 1
                expected = (date(year + 1, 1, 1) - date(year, 1, 1)).days
                missing_dates = [(date(year, 1, 1) + timedelta(days=i)).isoformat() for i in range(expected)
                                 if (date(year, 1, 1) + timedelta(days=i)).isoformat() not in labels]
                coverage[station][str(year)] = {"accepted_dates": count, "missing_dates": expected - count,
                                                "missing_target_dates": missing_dates}
            if progress:
                progress({"label_stations_processed": len(coverage), "accepted_station_dates": len(rows)})
    except Exception as error:
        errors.append({"reason": str(error) if isinstance(error, ValueError) else type(error).__name__})
    missing = sum(value["missing_dates"] for years_by_station in coverage.values() for value in years_by_station.values())
    report = {"mode": "offline_research", "role": "observations_only", "years": sorted(set(years)),
        "status": "partial_or_blocked" if errors else ("complete_with_gaps" if missing else "complete"),
        "accepted_station_date_events": len(rows), "missing_station_dates": missing,
        "coverage": coverage, "errors": errors, "requests_downloaded": cache.downloaded,
        "cache_hits": cache.hits, "dataset_hash": digest(rows), "completed_at": datetime.now(timezone.utc).isoformat(),
        "production_training_eligible": False, "forecast_error_training_pairs": 0}
    write_json(output / "dataset.json", rows)
    write_json(output / "latest.json", report)
    return report


def collect_all_stations(output, start, days, budget=250, cache=None, progress=None):
    """One all-station request per date, with reusable original-run receipts.

    Every location still counts toward provider usage. Pace 21-location batches
    at 2.5 seconds minimum (at most 504 location requests/minute, no retries).
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cache = cache or ReceiptCache(output / "receipts", budget, pace=2.5)
    stations = sorted(set(CLI_CODES.values()))
    targets = [start + timedelta(days=i) for i in range(days)]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": "offline_research",
              "status": "running", "scope": "all_21_registry_stations", "model": "gfs_global",
              "start_date": start.isoformat(), "end_date": targets[-1].isoformat(),
              "requested_dates": days, "requested_station_dates": days * len(stations),
              "live_ready": False, "paper_execution_enabled": False,
              "historical_public_availability_verified": False, "profitability_evaluated": False,
              "stations": {}, "errors": [], "dates_processed": 0,
              "limitations": ["Research-only physical-weather calibration; no production training or trading eligibility.",
                  "Hourly extrema approximate daily extrema; historical publication receipts are unverified.",
                  "Chronological evaluation is exploratory, not a permanently unseen holdout.",
                  "Nearby stations and dates can share weather; row counts are not independent samples."]}
    rows = []

    def checkpoint(final=False):
        report.update(updated_at=datetime.now(timezone.utc).isoformat(), requests_downloaded=cache.downloaded,
                      cache_hits=cache.hits, accepted_station_date_events=len(rows), dataset_hash=digest(rows))
        if final or report["dates_processed"] % 7 == 0:
            write_json(output / "dataset.json", rows)
        write_json(output / "latest.json", report)
        if progress:
            progress({key: report[key] for key in ("status", "dates_processed", "requested_dates",
                      "accepted_station_date_events", "requests_downloaded", "cache_hits")})

    checkpoint()
    try:
        inventory = cache.get(STATIONS, {"ids": ",".join(stations), "format": "json"})
        coordinates = {}
        for row in inventory["data"]:
            station = row.get("icaoId")
            if station in coordinates:
                raise ValueError("DUPLICATE_STATION_COORDINATES")
            coordinates[station] = row
        if any(station not in coordinates for station in stations):
            raise ValueError("STATION_COORDINATES_MISSING")
        locations = [coordinates[station] for station in stations]
        labels = {}
        for station in stations:
            labels[station] = {}
            for year in sorted({day.year for day in targets}):
                receipt = cache.get(CLIMATE, {"station": station, "year": year})
                labels[station].update({key: (value, receipt) for key, value in climate_labels(receipt, station).items()})
            report["stations"][station] = {"city": station_metadata(station)["city"],
                "station_coordinates": {key: coordinates[station].get(key) for key in ("lat", "lon", "elev")},
                "label_dates": sum(target.isoformat() in labels[station] for target in targets),
                "accepted_station_dates": 0}
        for target in targets:
            parameters = batch_parameters(locations, target)
            receipt = cache.get(ARCHIVE, parameters)
            forecasts = split_batch(receipt, locations)
            date_rows = []
            for index, station in enumerate(stations):
                if target.isoformat() not in labels[station]:
                    report["errors"].append({"station": station, "date": target.isoformat(), "reason": "CLI_LABEL_MISSING"})
                    continue
                label, label_receipt = labels[station][target.isoformat()]
                try:
                    example = make_example(station, target, parameters["run"], forecasts[index], label, label_receipt)
                except ValueError as error:
                    report["errors"].append({"station": station, "date": target.isoformat(), "reason": str(error)})
                    continue
                example.update(forecast_location_index=index, station_coordinates=report["stations"][station]["station_coordinates"])
                date_rows.append(example)
            # A date batch is accepted atomically, after identity validation.
            rows.extend(date_rows)
            for example in date_rows:
                report["stations"][example["station"]]["accepted_station_dates"] += 1
            report["dates_processed"] += 1
            checkpoint()
    except RequestBudgetExceeded:
        report["errors"].append({"reason": "REQUEST_BUDGET_EXHAUSTED_RESUME_WITH_SAME_CACHE"})
    except Exception as error:
        report["errors"].append({"reason": str(error) if isinstance(error, ValueError) else type(error).__name__})
    candidates = {}
    test_start = targets[int(days * .75)].isoformat()
    report["evaluation_test_start_date"] = test_start
    for station, summary in report["stations"].items():
        summary["evaluation"] = evaluate([row for row in rows if row["station"] == station], test_start)
        evaluation = summary["evaluation"]
        if evaluation["status"] == "exploratory_offline_only":
            candidates[station] = {"training_last_target_date": evaluation["train_dates"][-1],
                                  "training_station_dates": evaluation["train_station_date_events"],
                                  "bias_f": {metric: score["training_bias_f"] for metric, score in evaluation["scores"].items()}}
    status = "partial_or_blocked"
    if report["dates_processed"] == days:
        if not report["errors"]:
            status = "complete"
        elif all(error["reason"] in ("CLI_LABEL_MISSING", "INCOMPLETE_STANDARD_DAY") for error in report["errors"]):
            status = "complete_with_gaps"
    report.update(status=status, rejected_station_dates=sum("station" in error for error in report["errors"]),
                  completed_at=datetime.now(timezone.utc).isoformat())
    write_json(output / "calibration_candidates.json", {"mode": "offline_research", "model": "gfs_global",
               "production_training_eligible": False, "as_of_eligible": False, "stations": candidates})
    checkpoint(final=True)
    return report
