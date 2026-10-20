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


def evaluate(rows):
    """Date-grouped chronological split; high/low are not independent events."""
    dates = sorted({row["target_date"] for row in rows})
    if len(dates) < 14:
        return {"status": "insufficient_dates", "station_date_events": len(rows)}
    split = max(7, int(len(dates) * .75))
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
