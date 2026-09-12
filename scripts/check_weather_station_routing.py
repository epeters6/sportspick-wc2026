"""Read-only public station/day/partition smoke check; never calls a forecast or ledger.

Run with python -m scripts.check_weather_station_routing. Uses eight bounded
Kalshi series reads and one Polymarket US public search; BBO reads are bounded
to the first current/future event for each supported Polymarket station.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests


def main():
    os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "pavlov"))
    from pavlov.pipeline import kalshi_client
    from pavlov.polymarket import poly_client
    from pavlov.pipeline.settlement_resolver import normalize_market
    from pavlov.pipeline.station_mapper import _VERIFIED_KALSHI_SERIES

    counts, failures, groups = Counter(), Counter(), defaultdict(list)

    def accept(row, venue):
        if row is None:
            failures[f"{venue}:FLATTEN_FAILED"] += 1
            return
        try:
            event = normalize_market(row, venue, require_verified_station=True)
            if event is None:
                failures[f"{venue}:BUCKET_OR_DATE_FAILED"] += 1
                return
            counts[f"{venue}:{event.settlement_station}"] += 1
            groups[(venue, event.settlement_station, event.date.isoformat(),
                    event.settlement_source, event.observation_timezone)].append(event)
        except ValueError as exc:
            failures[f"{venue}:{str(exc).split(':')[0]}"] += 1

    for series in _VERIFIED_KALSHI_SERIES:
        response = requests.get("https://external-api.kalshi.com/trade-api/v2/markets",
                                params={"series_ticker": series, "status": "open", "limit": 100}, timeout=20)
        response.raise_for_status()
        for raw in response.json().get("markets", []):
            accept(kalshi_client._parse_market(raw, series_ticker=series), "kalshi")

    response = requests.get("https://gateway.polymarket.us/v1/search",
                            params={"query": "temperature", "limit": 30}, timeout=20)
    response.raise_for_status()
    public_client = poly_client.get_public_client()
    seen_series = set()
    today = datetime.now(timezone.utc).date().isoformat()
    for event in response.json().get("events", []):
        slug = str(event.get("slug") or "")
        if len(slug) < 10 or slug[-10:] < today or not slug.startswith("temp-"):
            continue
        series = slug[:-11]
        if series in seen_series:
            continue
        seen_series.add(series)
        for raw in event.get("markets", []):
            raw = {**raw, "eventSlug": raw.get("eventSlug") or slug}
            bbo = poly_client._build_price_bbo(public_client, raw, raw["slug"])
            accept(poly_client._normalize_market_row(public_client, raw, bbo,
                         display_text=poly_client._poly_market_display_text(raw)), "polymarket")
        if len(seen_series) >= 5:
            break

    partitions = []
    for key, events in sorted(groups.items()):
        bounds = sorted((e.bucket_low_f, e.bucket_high_f) for e in events)
        valid = (bounds[0][0] == float("-inf") and bounds[-1][1] == float("inf")
                 and all(lo < hi for lo, hi in bounds)
                 and all(left[1] == right[0] for left, right in zip(bounds, bounds[1:])))
        partitions.append({"venue": key[0], "station": key[1], "date": key[2],
                           "source": key[3], "observation_timezone": key[4],
                           "buckets": len(bounds), "complete": valid})
    print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
                      "normalized": dict(counts), "failures": dict(failures),
                      "partitions": partitions}, indent=2))


if __name__ == "__main__":
    main()
