"""Bounded public discovery around the unchanged v3 forecast implementation."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from backend.ml.weather_learning import VERSION
from backend.ml.weather_learning.collection import DECISION_HOURS
from backend.ml.weather_learning.contracts import CLI_CODES, station_metadata
from backend.ml.weather_learning.dataset import digest, utc
from backend.ml.weather_learning.feeds import PublicFeeds


class CollectionFeeds(PublicFeeds):
    """Keep the existing pacing/retries, but stop network work before job expiry."""
    deadline = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rate_limited_hosts = set()

    def fetch(self, url, params, *, ttl=1200):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ValueError("COLLECTION_TIME_BUDGET_EXHAUSTED")
        host = urlsplit(url).netloc
        if host in self.rate_limited_hosts:
            raise ValueError("PUBLIC_FEED_HOST_RATE_LIMITED")
        path = self.path / (digest({"url": url, "params": params}) + ".json")
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (saved.get("url") != url or saved.get("params") != params
                    or digest(saved["data"]) != saved.get("content_hash")
                    or utc(saved["received_at"]) > self.clock()):
                raise ValueError("CORRUPT_PUBLIC_FEED_RECEIPT")
        try:
            return super().fetch(url, params, ttl=ttl)
        except RuntimeError as exc:
            if str(exc) == "PUBLIC_FEED_RATE_LIMIT":
                self.rate_limited_hosts.add(host)
            raise


def eligible_hours(now):
    return any(now.astimezone(ZoneInfo(station_metadata(s)["timezone"])).hour in DECISION_HOURS
               for s in set(CLI_CODES.values()))


def pages(feeds, resource, size):
    """Fail closed on malformed/truncated catalogs; never price catalog rows."""
    result, receipts, seen = [], [], set()
    for offset in range(0, 4001, size):
        receipt = feeds.fetch("https://gateway.polymarket.us/v1/" + resource,
                              {"limit": size, "offset": offset, "active": "true", "closed": "false",
                               "categories": ["climate"]}, ttl=120)
        data = receipt["data"]
        rows = data.get(resource) if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError("MALFORMED_POLYMARKET_CATALOG")
        slugs = [r.get("slug") for r in rows]
        if any(not s for s in slugs) or len(set(slugs)) != len(slugs) or seen.intersection(slugs):
            raise ValueError("DUPLICATE_OR_MISSING_POLYMARKET_CATALOG_ID")
        seen.update(slugs)
        result.extend(rows)
        receipts.append({"resource": resource, "category": "climate", "received_at": receipt["received_at"], "content_hash": receipt["content_hash"]})
        if len(rows) < size:
            return result, receipts
    raise ValueError("POLYMARKET_DISCOVERY_PAGINATION_LIMIT")


def poly_catalog(feeds):
    from pavlov.polymarket.poly_client import (
        _flatten_event_markets, _merge_market_rows, _poly_market_display_text,
        _poly_outcome_text, _looks_like_daily_temp, _poly_strike_fields,
    )
    flat, receipts = pages(feeds, "markets", 400)
    events, event_receipts = pages(feeds, "events", 200)
    receipts.extend(event_receipts)
    event_end = {e.get("slug"): e.get("endTime") or e.get("endDate") for e in events}
    rows = _merge_market_rows(flat, _flatten_event_markets(events))
    result = []
    for row in rows:
        title, outcome = _poly_market_display_text(row), _poly_outcome_text(row)
        full = f"{title} ({outcome})" if outcome else title
        if not row.get("slug") or not _looks_like_daily_temp(full):
            continue
        strike = _poly_strike_fields(full)
        if not strike:
            continue
        # Contract discovery needs rules and identity only. Executable quotes
        # still come from the frozen, fresh-book identity/state checks later.
        end = event_end.get(row.get("eventSlug")) or row.get("endDate") or row.get("end_time") or ""
        result.append({"ticker": row["slug"], "title": full, "market_title": full,
            "description": row.get("description", ""), "rules_primary": row.get("rules_primary", ""),
            "resolution_source": row.get("resolutionSource") or row.get("resolution_source"),
            "settlement_station": row.get("settlement_station") or row.get("station"),
            "close_time": end, "poly_event_slug": row.get("eventSlug", ""),
            "poly_market_slug": row["slug"], "venue": "poly_us", "_platform": "polymarket",
            "strike_type": strike["strike_type"], "floor_strike": strike.get("floor_strike"),
            "ceiling_strike": strike.get("ceiling_strike"), "threshold_lo": strike.get("threshold_lo"),
            "threshold_hi": strike.get("threshold_hi")})
    return result, receipts


def discover_catalog(feeds):
    # Legacy Kalshi imports use the standalone Pavlov config module. Preserve
    # the frozen discovery entry point's initialization in a fresh process.
    os.environ["PAVLOV_BYPASS_CONFIG"] = "1"
    pavlov_path = str(Path(__file__).resolve().parents[1] / "pavlov")
    if pavlov_path not in sys.path:
        sys.path.insert(0, pavlov_path)
    from pavlov.pipeline.kalshi_client import _WEATHER_SERIES_TICKERS, _parse_market
    markets, errors, receipts = [], [], []
    for series in _WEATHER_SERIES_TICKERS:
        try:
            page = feeds.fetch("https://external-api.kalshi.com/trade-api/v2/markets",
                               {"series_ticker": series, "status": "open", "limit": 100}, ttl=120)
            if page["data"].get("cursor"):
                raise ValueError("DISCOVERY_PAGINATION_REQUIRED")
            raw_markets = page["data"].get("markets")
            if not isinstance(raw_markets, list) or any(not isinstance(r, dict) for r in raw_markets):
                raise ValueError("MALFORMED_KALSHI_CATALOG")
            receipts.append({"series": series, "received_at": page["received_at"], "content_hash": page["content_hash"]})
            parsed = [{**_parse_market({**raw, "received_timestamp": page["received_at"]}, series), "_platform": "kalshi"}
                      for raw in raw_markets]
            markets.extend(parsed)
        except Exception as exc:
            errors.append({"venue": "kalshi", "series": series,
                           "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    try:
        poly, poly_receipts = poly_catalog(feeds)
        markets.extend(poly)
        receipts.extend(poly_receipts)
        if not poly:
            errors.append({"venue": "polymarket", "reason": "NO_CURRENT_MARKETS"})
    except Exception as exc:
        errors.append({"venue": "polymarket", "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    return markets, errors, receipts


def collect_current(output, *, collector, db=None, artifact=None, feeds=None, clock=None):
    """Only optimize acquisition; the frozen collector owns all forecasts/claims."""
    output = Path(output)
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    feeds = feeds or CollectionFeeds(output / "cache", clock=clock)
    # Cheap duplicate preflight avoids catalog calls. The collector's atomic
    # claim remains authoritative if another worker starts during discovery.
    if db is not None:
        key = "weather_learning_slot_v3:" + started.strftime("%Y%m%dT%H")
        prior = db.table("app_settings").select("value").eq("key", key).execute().data or []
        if prior:
            if len(prior) != 1 or not prior[0].get("value", {}).get("run_id"):
                raise ValueError("MALFORMED_COLLECTION_SLOT")
            return {"version": VERSION, "started_at": started.isoformat(), "status": "skipped",
                    "reason": "HOURLY_SLOT_ALREADY_ATTEMPTED", "snapshots": 0, "errors": [],
                    "mode": "shadow", "live_ready": False, "paper_execution_enabled": False}
    outside = not eligible_hours(started)
    markets, errors, receipts = ([], [], []) if outside else discover_catalog(feeds)
    # Anchor eligibility to this invocation's start even if discovery crosses
    # an hour; final quote/decision timestamps still use the real clock.
    times = iter([started])
    report = collector(output, db=db, artifact=artifact, feeds=feeds, markets=markets,
                       clock=lambda: next(times, None) or clock())
    report.setdefault("errors", []).extend(errors)
    if errors:
        report["status"] = "degraded"
    report["acquisition"] = {"catalog_only": True, "outside_all_station_windows": outside,
        "receipts": receipts, "runtime_hash": digest(Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n"))}
    output.mkdir(parents=True, exist_ok=True)
    (output / "collection.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report


def guard_inputs(output, collection, started):
    """An intentional duplicate skip never consumes a stale or missing artifact."""
    if collection.get("status") == "skipped" and collection.get("reason") == "HOURLY_SLOT_ALREADY_ATTEMPTED":
        if collection.get("snapshots", 0) or collection.get("errors"):
            raise ValueError("INCONSISTENT_COLLECTION_SKIP")
        return None
    if not collection:
        raise ValueError("COLLECTION_REPORT_MISSING")
    rows = [json.loads(line) for line in (Path(output) / "snapshots.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if len(rows) != collection["snapshots"]:
        raise ValueError("COLLECTION_ARTIFACT_COUNT_MISMATCH")
    if any(utc(r["metadata"]["decision_at"]) < started for r in rows):
        raise ValueError("STALE_COLLECTION_FILE_FOR_MARKET_GUARD")
    if collection.get("run_id") and any(r["metadata"].get("run_id") != collection["run_id"] for r in rows):
        raise ValueError("COLLECTION_ARTIFACT_RUN_MISMATCH")
    return rows
