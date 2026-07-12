"""
Weather verification writer — the training-data pipeline for the MOS bias engine.

Two halves:
  1. record_prediction(): called at prediction time (sync_weather) to upsert the
     raw ensemble forecast for a station/date/lead-time.
  2. backfill_actuals(): called after settlement to fill in the observed
     high/low from the METAR archive, completing predicted-vs-actual rows that
     backend.ml.weather_mos.WeatherMOS trains on.
"""
import logging
import os
import sys
from datetime import datetime, timezone, timedelta, date as date_type

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.db import get_db

logger = logging.getLogger(__name__)

_pavlov_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "pavlov")
if _pavlov_path not in sys.path:
    sys.path.insert(0, _pavlov_path)


def record_prediction(
    station_id: str,
    lead_time_days: int,
    target_date: str,
    predicted: float,
    metric: str = "high",
    model_name: str = "ensemble",
) -> bool:
    """Upsert the raw (pre-MOS) model forecast for later verification.

    Stores the UNCORRECTED forecast so the learned bias stays stationary —
    if we stored the bias-corrected value, MOS would chase its own tail.
    """
    db = get_db()
    col = "predicted_high" if metric == "high" else "predicted_low"
    try:
        db.table("weather_verification").upsert(
            {
                "station_id": station_id,
                "lead_time_days": max(int(lead_time_days), 0),
                "model_name": model_name,
                "target_date": target_date,
                col: round(float(predicted), 1),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="station_id,lead_time_days,model_name,target_date",
        ).execute()
        return True
    except Exception as exc:
        logger.warning(f"weather_verification upsert failed for {station_id} {target_date}: {exc}")
        return False


def _station_city(station_id: str) -> str | None:
    from pipeline.station_mapper import STATION_MAP

    for city, meta in STATION_MAP.items():
        if meta.get("station") == station_id:
            return city
    return None


def fetch_actual_extremes(station_id: str, target_date: str) -> dict:
    """Observed high/low (°F) for a station on a station-local calendar date.

    Uses the Aviation Weather Center METAR archive (~96h of history). Returns
    {"high": float|None, "low": float|None}.
    """
    import requests
    from zoneinfo import ZoneInfo
    from pipeline.station_mapper import get_tz_for_city

    out: dict = {"high": None, "low": None}
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return out

    now = datetime.now(timezone.utc)
    hours_ago = int((now - datetime(target.year, target.month, target.day, tzinfo=timezone.utc)).total_seconds() / 3600)
    hours_back = min(96, max(24, hours_ago + 30))

    url = (
        f"https://aviationweather.gov/api/data/metar?ids={station_id}"
        f"&format=json&taf=false&hours={hours_back}"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug(f"METAR history fetch failed for {station_id} {target_date}: {exc}")
        return out

    if not isinstance(data, list):
        return out

    city = _station_city(station_id)
    tz = ZoneInfo(get_tz_for_city(city)) if city else timezone.utc

    temps: list[float] = []
    for obs in data:
        temp_c = obs.get("temp")
        report_time = obs.get("reportTime", "")
        if temp_c is None or not report_time:
            continue
        try:
            obs_dt = datetime.fromisoformat(report_time.replace("Z", "+00:00"))
            if obs_dt.tzinfo is None:
                obs_dt = obs_dt.replace(tzinfo=timezone.utc)
            if obs_dt.astimezone(tz).date() == target:
                temps.append(float(temp_c) * 9 / 5 + 32)
        except (ValueError, TypeError):
            continue

    if temps:
        out["high"] = round(max(temps), 1)
        out["low"] = round(min(temps), 1)
    return out


def backfill_actuals(max_rows: int = 100) -> int:
    """Fill actual_high/actual_low on verification rows whose target date has passed.

    Only attempts dates within the METAR archive window (~4 days) — older rows
    without actuals are left alone (they simply never become training data).
    """
    db = get_db()
    today = datetime.now(timezone.utc).date()
    oldest = today - timedelta(days=4)

    try:
        rows = (
            db.table("weather_verification")
            .select("*")
            .is_("actual_high", "null")
            .gte("target_date", oldest.isoformat())
            .lt("target_date", today.isoformat())
            .limit(max_rows)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning(f"weather_verification query failed: {exc}")
        return 0

    # One METAR fetch per (station, date), shared across lead-time rows
    actuals_cache: dict[tuple[str, str], dict] = {}
    updated = 0

    for row in rows:
        station = row.get("station_id")
        tdate = row.get("target_date")
        if not station or not tdate:
            continue
        if isinstance(tdate, date_type):
            tdate = tdate.isoformat()

        key = (station, tdate)
        if key not in actuals_cache:
            actuals_cache[key] = fetch_actual_extremes(station, tdate)
        actual = actuals_cache[key]

        patch = {}
        if actual.get("high") is not None and row.get("actual_high") is None:
            patch["actual_high"] = actual["high"]
        if actual.get("low") is not None and row.get("actual_low") is None:
            patch["actual_low"] = actual["low"]
        if not patch:
            continue

        patch["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            db.table("weather_verification").update(patch).eq("id", row["id"]).execute()
            updated += 1
        except Exception as exc:
            logger.warning(f"weather_verification update failed for {row.get('id')}: {exc}")

    if updated:
        logger.info(f"weather_verification: backfilled actuals on {updated} rows.")
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n = backfill_actuals()
    print(f"Backfilled {n} verification rows.")
