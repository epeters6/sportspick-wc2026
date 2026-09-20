"""Public weather inputs with receipt timestamps, shared caches and bounded retries."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .dataset import digest, utc

MODELS = {"gfs_global": "gfs", "gfs_hrrr": "hrrr", "ecmwf_ifs025": "ecmwf"}
VARIABLES = "temperature_2m,dew_point_2m,cloud_cover,wind_speed_10m,wind_direction_10m"


class PublicFeeds:
    def __init__(self, cache_dir: Path, session=None, clock=None):
        self.path = cache_dir
        self.path.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_request = 0.0
        self.errors = []

    def fetch(self, url, params, *, ttl=1200):
        identity = digest({"url": url, "params": params})
        path = self.path / (identity + ".json")
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            age = (self.clock() - utc(saved["received_at"])).total_seconds()
            if 0 <= age <= ttl and digest(saved["data"]) == saved["content_hash"]:
                return saved
        elapsed = time.monotonic() - self.last_request
        if elapsed < 1.2:
            time.sleep(1.2 - elapsed)
        for attempt in range(2):
            try:
                self.last_request = time.monotonic()
                response = self.session.get(url, params=params, timeout=20,
                                            headers={"User-Agent": "weather-station-research/3 public-data"})
                if response.status_code == 429:
                    raise RuntimeError("PUBLIC_FEED_RATE_LIMIT")
                response.raise_for_status()
                data = response.json()
                saved = {"url": url, "params": params, "data": data,
                         "received_at": self.clock().isoformat(), "content_hash": digest(data)}
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(saved, allow_nan=False), encoding="utf-8")
                temp.replace(path)
                return saved
            except requests.RequestException:
                if attempt:
                    raise
                time.sleep(2)

    def observations(self, stations):
        # Batch station requests rather than fetching every neighbor separately.
        return self.fetch("https://aviationweather.gov/api/data/metar", {
            "ids": ",".join(sorted(set(stations))), "format": "json", "hours": 30,
        }, ttl=300)

    def station(self, code):
        result = self.fetch("https://aviationweather.gov/api/data/stationinfo", {"ids": code, "format": "json"}, ttl=30 * 86400)
        matches = [r for r in result["data"] if r.get("icaoId") == code]
        if len(matches) != 1:
            raise ValueError("STATION_COORDINATES_NOT_VERIFIED")
        row = matches[0]
        return {"station": code, "lat": float(row["lat"]), "lon": float(row["lon"]),
                "elevation_m": row.get("elev"), "source": result["url"], "content_hash": result["content_hash"]}

    def forecast(self, station):
        return self.fetch("https://api.open-meteo.com/v1/forecast", {
            "latitude": station["lat"], "longitude": station["lon"],
            "elevation": station["elevation_m"], "hourly": VARIABLES,
            "models": ",".join(MODELS), "forecast_days": 4, "past_days": 1,
            "temperature_unit": "fahrenheit", "timeformat": "unixtime", "timezone": "GMT",
        }, ttl=1800)

    def archived_forecast(self, station, run: str, model="gfs_global"):
        """Explicit operational run archive. Never backdate a live forecast.

        Initialization is not publication. The importer must supply independently
        verified availability before an archived run becomes an as-of feature.
        """
        if model not in MODELS:
            raise ValueError("UNSUPPORTED_ARCHIVE_MODEL")
        return self.fetch("https://single-runs-api.open-meteo.com/v1/forecast", {
            "latitude": station["lat"], "longitude": station["lon"], "elevation": station["elevation_m"],
            "run": run, "models": model, "hourly": VARIABLES, "forecast_days": 3,
            "temperature_unit": "fahrenheit", "timeformat": "unixtime", "timezone": "GMT",
        }, ttl=365 * 86400)

