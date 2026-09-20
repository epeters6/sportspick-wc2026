"""Station observations condition an hourly forecast path, never a daily-max delta."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import mean, pstdev

from .contracts import NEIGHBORS, Contract
from .dataset import finite, utc
from .feeds import MODELS


def station_observations(payload, station, start, end, as_of):
    rows = []
    # Receipt time is the earliest time this collector can claim it knew data.
    if utc(payload["received_at"]) > as_of:
        raise ValueError("OBSERVATIONS_RECEIVED_AFTER_DECISION")
    for row in payload["data"]:
        if row.get("icaoId") != station:
            continue
        try:
            stamp = datetime.fromtimestamp(float(row["obsTime"]), timezone.utc) if row.get("obsTime") is not None else utc(row["reportTime"])
            received = datetime.fromtimestamp(float(row["receiptTime"]), timezone.utc) if isinstance(row.get("receiptTime"), (int, float)) else utc(row.get("receiptTime") or row["reportTime"])
            temp = finite(row.get("temp"))
            if temp is not None and start <= stamp < end and stamp <= as_of and received <= as_of:
                rows.append({**row, "stamp": stamp, "temp_f": temp * 1.8 + 32})
        except (KeyError, ValueError, TypeError):
            continue
    # METAR/SPECI revisions at a timestamp are not independent observations.
    return sorted({r["stamp"]: r for r in rows}.values(), key=lambda r: r["stamp"])


def trend(rows):
    if len(rows) < 2:
        return None
    last = rows[-1]
    recent = [r for r in rows if 0 < (last["stamp"] - r["stamp"]).total_seconds() <= 3 * 3600]
    if not recent:
        return None
    first = recent[0]
    hours = (last["stamp"] - first["stamp"]).total_seconds() / 3600
    return (last["temp_f"] - first["temp_f"]) / hours if hours >= .25 else None


def weather_features(contract: Contract, forecast: dict, observations: dict, as_of: datetime):
    start, end = utc(contract.window_start), utc(contract.window_end)
    if utc(forecast["received_at"]) > as_of:
        raise ValueError("FORECAST_RECEIVED_AFTER_DECISION")
    hourly = forecast["data"]["hourly"]
    times = [datetime.fromtimestamp(float(t), timezone.utc) for t in hourly["time"]]
    indices = [i for i, stamp in enumerate(times) if start <= stamp < end]
    if len(indices) != 24:
        raise ValueError("INCOMPLETE_FORECAST_OBSERVATION_DAY")
    obs = station_observations(observations, contract.station, start, end, as_of)
    latest = obs[-1] if obs else None
    age = (as_of - latest["stamp"]).total_seconds() / 60 if latest else None
    fresh = latest is not None and age <= 90
    feature = {"remaining_hours": max(0, (end - max(as_of, start)).total_seconds() / 3600),
               "forecast_age_minutes": (as_of - utc(forecast["received_at"])).total_seconds() / 60,
               "observation_count": len(obs), "observation_age_minutes": age,
               "day_coverage_fraction": len({r["stamp"].hour for r in obs}) / max(1, min(24, (min(as_of, end) - start).total_seconds() / 3600)) if obs else 0.0}
    if obs:
        feature.update(observed_high_f=max(r["temp_f"] for r in obs), observed_low_f=min(r["temp_f"] for r in obs))
    if fresh:
        direction = finite(latest.get("wdir"))
        feature.update(observed_temp_f=latest["temp_f"], temperature_trend_f_hour=trend(obs),
                       dewpoint_f=(finite(latest.get("dewp")) * 1.8 + 32) if finite(latest.get("dewp")) is not None else None,
                       wind_speed_kt=finite(latest.get("wspd")),
                       wind_sin=math.sin(math.radians(direction)) if direction is not None else None,
                       wind_cos=math.cos(math.radians(direction)) if direction is not None else None)
    raw_extremes, adjusted_extremes, clouds = [], [], []
    for model, short in MODELS.items():
        values = hourly.get("temperature_2m_" + model, [])
        if len(values) != len(times) or any(finite(values[i]) is None for i in indices):
            continue
        day = [float(values[i]) for i in indices]
        feature[short + "_high_f"], feature[short + "_low_f"] = max(day), min(day)
        extreme = max if contract.metric == "high" else min
        raw_extremes.append(extreme(day))
        shift = 0.0
        if fresh:
            # Interpolate the forecast at the observation time, not its daily max.
            before = [i for i, stamp in enumerate(times) if stamp <= latest["stamp"]]
            if before and before[-1] + 1 < len(times):
                i = before[-1]
                left, right = finite(values[i]), finite(values[i + 1])
                if left is not None and right is not None:
                    fraction = (latest["stamp"] - times[i]).total_seconds() / 3600
                    expected = left + fraction * (right - left)
                    shift = max(-6.0, min(6.0, latest["temp_f"] - expected))
        future = [float(values[i]) + shift * math.exp(-max(0, (times[i] - as_of).total_seconds() / 3600) / 6)
                  for i in indices if times[i] >= as_of]
        if obs and fresh:
            # Provisional observed extreme participates in the path estimate;
            # probabilities remain soft because the final reporting source differs.
            future.append(extreme(r["temp_f"] for r in obs))
        adjusted_extremes.append(extreme(future) if future and fresh else extreme(day))
        near = min(indices, key=lambda i: abs((times[i] - as_of).total_seconds()))
        cloud = hourly.get("cloud_cover_" + model, [])
        if len(cloud) == len(times) and finite(cloud[near]) is not None:
            clouds.append(float(cloud[near]))
    if not raw_extremes:
        raise ValueError("NO_COMPLETE_WEATHER_MODEL")
    feature.update(forecast_mean_f=mean(raw_extremes), nowcast_mean_f=mean(adjusted_extremes),
                   model_disagreement_f=pstdev(raw_extremes), model_count=len(raw_extremes),
                   nowcast_adjustment_f=mean(adjusted_extremes) - mean(raw_extremes))
    if clouds:
        feature["cloud_cover_pct"] = mean(clouds)
    neighbors = []
    neighbor_trends = []
    if fresh:
        for station in NEIGHBORS.get(contract.station, []):
            nearby = station_observations(observations, station, start, end, as_of)
            if nearby and (as_of - nearby[-1]["stamp"]).total_seconds() <= 5400:
                neighbors.append(nearby[-1]["temp_f"] - latest["temp_f"])
                slope = trend(nearby)
                if slope is not None:
                    neighbor_trends.append(slope)
    feature["neighbor_temperature_delta_f"] = mean(neighbors) if neighbors else None
    feature["neighbor_trend_f_hour"] = mean(neighbor_trends) if neighbor_trends else None
    # Diagnostic prior, not a learned or approved execution distribution. A
    # separate supervised model learns the venue outcome from these features.
    sigma = max(1.5, pstdev(adjusted_extremes), 1.5 + .10 * feature["remaining_hours"])
    feature["prior_sigma_f"] = sigma
    mu = feature["nowcast_mean_f"]
    cdf = lambda x: .5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))
    feature["nowcast_probability"] = ((cdf(contract.high_f) if contract.high_f is not None else 1.0)
                                       - (cdf(contract.low_f) if contract.low_f is not None else 0.0))
    return feature
