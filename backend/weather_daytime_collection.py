"""Public, bounded collection and official grading for the daytime paper cohort."""
from __future__ import annotations

import copy
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from backend import weather_daytime_paper as paper
from backend.ml.weather_learning.collection import discover, normalized_quote, refresh_quotes
from backend.ml.weather_learning.contracts import resolve, station_metadata
from backend.ml.weather_learning.dataset import utc, digest
from backend.ml.weather_learning.outcomes import final_result
from backend.ml.weather_learning.feeds import PublicFeeds
from backend.weather_historical_calibration import ARCHIVE, ReceiptCache


class StrictPublicFeeds(PublicFeeds):
    def fetch(self, url, params, *, ttl=1200):
        path = self.path / (digest({"url": url, "params": params}) + ".json")
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (saved.get("url") != url or saved.get("params") != params
                    or digest(saved["data"]) != saved.get("content_hash")
                    or utc(saved["received_at"]) > self.clock()):
                raise ValueError("DAYTIME_PUBLIC_RECEIPT_CORRUPT")
        return super().fetch(url, params, ttl=ttl)


def official_outcomes(event, feeds, clock):
    outcomes, receipts = [], []
    for c in event["contracts"]:
        market_id, venue = c["market_id"], c["platform"]
        if venue == "kalshi":
            response = feeds.fetch("https://external-api.kalshi.com/trade-api/v2/markets/" + market_id, {}, ttl=3600)
            market = response["data"].get("market", {})
            rules = "\n".join(str(market.get(k) or "") for k in ("rules_primary", "rules_secondary", "description", "resolution_source")).strip()
            if rules != c["rules_text"]:
                raise ValueError("DAYTIME_FINAL_CONTRACT_CHANGED")
        elif venue == "polymarket":
            response = feeds.fetch("https://gateway.polymarket.us/v1/markets/" + market_id + "/settlement", {}, ttl=3600)
            market = response["data"]
        else:
            raise ValueError("DAYTIME_UNSUPPORTED_VENUE")
        if (digest(response["data"]) != response["content_hash"]
                or not utc(event["decision_at"]) < utc(response["received_at"]) <= clock()):
            raise ValueError("DAYTIME_FINAL_RECEIPT_INVALID")
        result = final_result(venue, market_id, market)
        if result is None:
            return None, []
        outcomes.append(int(result == "yes")); receipts.append(response)
    return outcomes, receipts


def grade(store, book, feeds, clock, report):
    now = clock()
    pending = sorted((r["next_label_check"], key) for key, r in book["events"].items()
                     if r["status"] != "settled" and utc(r["next_label_check"]) < now)
    # A disputed old market cannot monopolize the queue. Checks wait six hours.
    for _, key in pending[:12]:
        try:
            event = store.get("decision", key)
            paper.verify_signed(event, "fingerprint")
            if event["fingerprint"] != book["events"][key]["fingerprint"]:
                raise ValueError("DAYTIME_DECISION_JOURNAL_CONFLICT")
            outcomes, receipts = official_outcomes(event, feeds, clock)
            if outcomes is not None:
                book = store.update(book, paper.settle(book, event, outcomes, receipts, clock()))
                book = store.flush(book)
                report["graded"] += 1
                continue
        except Exception as exc:
            report["errors"].append({"stage": "grade", "id": key, "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
        updated = copy.deepcopy(book)
        updated["events"][key]["next_label_check"] = (now + timedelta(hours=6)).isoformat()
        book = store.update(book, paper.signed(updated, "state_hash"))
    return book


def run(store, feeds, *, clock=None, markets=None, archive_cache=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    book = store.flush(store.load())
    report = {"started_at": started.isoformat(), "captured": 0, "graded": 0, "errors": [], "rejected": {}}
    book = grade(store, book, feeds, clock, report)
    eligible_stations = []
    for s in paper.SPEC["stations"]:
        local = started.astimezone(ZoneInfo(station_metadata(s)["timezone"]))
        target = (local.date() + timedelta(days=1)).isoformat()
        if paper.eligible(s, target, started) and any(f"{v}:{s}:{target}" not in book["events"] for v in ("kalshi", "polymarket")):
            eligible_stations.append(s)
    if eligible_stations:
        if markets is None:
            markets, errors = discover(feeds)
            report["errors"].extend(errors)
        groups, rejected = defaultdict(list), Counter()
        for raw in markets:
            try:
                c = resolve(raw, raw["_platform"])
                key = f"{c.platform}:{c.station}:{c.target_date}"
                if c.metric != "high" or c.station not in eligible_stations or not paper.eligible(c.station, c.target_date, started):
                    rejected["OUTSIDE_DAYTIME_COHORT"] += 1
                elif key not in book["events"]:
                    groups[key].append((c, raw))
            except (ValueError, KeyError, TypeError) as exc:
                rejected[str(exc)] += 1
        cache = archive_cache or ReceiptCache(store.output / "gfs_receipts", budget=21)
        forecasts = {}
        # Original-run errors stop further provider requests; no retries.
        provider_failed = False
        for key, pairs in sorted(groups.items()):
            try:
                pairs.sort(key=lambda x: float("-inf") if x[0].low_f is None else x[0].low_f)
                contracts = [x[0] for x in pairs]
                c = contracts[0]
                identity = (c.station, c.target_date)
                if identity not in forecasts:
                    if provider_failed or (book.get("provider_retry_after") and clock() < utc(book["provider_retry_after"])):
                        raise ValueError("DAYTIME_FORECAST_PROVIDER_STOPPED")
                    location = store.model["locations"][c.station]
                    params = {"latitude": location["lat"], "longitude": location["lon"], "elevation": location["elev"],
                        "run": (datetime.fromisoformat(c.target_date).date() - timedelta(days=1)).isoformat() + "T00:00",
                        "models": "gfs_global", "hourly": "temperature_2m", "forecast_days": 3,
                        "temperature_unit": "fahrenheit", "timeformat": "unixtime", "timezone": "GMT"}
                    try:
                        forecasts[identity] = cache.get(ARCHIVE, params)
                    except Exception:
                        provider_failed = True
                        updated = copy.deepcopy(book)
                        updated["provider_retry_after"] = (clock() + timedelta(hours=24)).isoformat()
                        book = store.update(book, paper.signed(updated, "state_hash"))
                        raise
                quotes = [normalized_quote(q) for q in refresh_quotes([x[1] for x in pairs], c.platform, feeds)]
                if len(quotes) != len(contracts) or any(resolve(q, c.platform) != old for q, old in zip(quotes, contracts)):
                    raise ValueError("DAYTIME_CONTRACT_CHANGED_DURING_CAPTURE")
                event = paper.decision(store.model, contracts, quotes, forecasts[identity], clock())
                book = store.update(book, paper.reserve(book, event))
                book = store.flush(book)
                report["captured"] += 1
            except Exception as exc:
                # A pending journal is recovered before doing any further work.
                book = store.flush(store.load())
                report["errors"].append({"stage": "capture", "id": key,
                    "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
        report["rejected"] = dict(rejected)
        report["gfs_downloads"] = cache.downloaded
    report.update(**paper.summary(book), completed_at=clock().isoformat())
    report["status"] = "degraded" if report["errors"] else "healthy"
    store.output.mkdir(parents=True, exist_ok=True)
    (store.output / "latest.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    store.db.table("app_settings").upsert({"key": paper.SOURCE + "_latest", "value": report}, on_conflict="key").execute()
    saved = store.db.table("app_settings").select("value").eq("key", paper.SOURCE + "_latest").execute().data or []
    if len(saved) != 1 or saved[0]["value"] != report:
        raise ValueError("DAYTIME_REPORT_NOT_PERSISTED")
    return report
