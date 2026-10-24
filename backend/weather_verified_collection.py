"""Bounded exact-run capture with its own durable quota and shadow journal."""
from __future__ import annotations

import copy
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from backend import weather_daytime_paper as paper
from backend import weather_verified_runs as runs
from backend import weather_verified_shadow as shadow
from backend.ml.weather_learning.collection import normalized_quote, refresh_quotes
from backend.ml.weather_learning.contracts import resolve, station_metadata
from backend.ml.weather_learning.dataset import digest
from backend.weather_collection_runtime import discover_catalog
from backend.weather_daytime_collection import official_outcomes
from backend.weather_historical_calibration import ARCHIVE


class Forecasts:
    def __init__(self, store, clock, *, session=None, sleep=time.sleep):
        self.store, self.clock = store, clock
        self.session, self.sleep = session or requests.Session(), sleep
        self.downloads, self.metadata = 0, {}

    def fetch(self, url, params, *, metadata=False):
        # All endpoints are fixed by the caller, never taken from market content.
        if url != ARCHIVE and url not in [runs.metadata_url(m) for m in runs.MODEL_DOMAINS]:
            raise ValueError("SHADOW_PROVIDER_URL")
        if not metadata:
            saved = self.store.receipt(url, params)
            if saved is not None:
                if runs.timestamp(saved["received_at"]) > self.clock():
                    raise ValueError("SHADOW_FUTURE_CACHE")
                return saved
        state, now = self.store.load(), self.clock()
        if state["provider_retry_after"] and now < runs.timestamp(state["provider_retry_after"]):
            raise ValueError("SHADOW_PROVIDER_COOLDOWN")
        if not metadata:
            if self.downloads >= shadow.SPEC["max_downloads_per_invocation"]:
                raise ValueError("SHADOW_DOWNLOAD_BUDGET")
            updated = shadow.reserve_download(state, digest({"url": url, "params": params}), now)
            self.downloads += 1
        else:
            updated = paper.signed({**state, "provider_retry_after": (now+timedelta(hours=24)).isoformat()}, "state_hash")
        state = self.store.update(state, updated)  # Before network; an interrupted attempt stays charged.
        self.sleep(1.3)
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("error"):
                raise ValueError("SHADOW_PROVIDER_INVALID_RESPONSE")
            receipt = {"url": url, "params": params, "received_at": self.clock().isoformat(),
                       "data": data, "content_hash": digest(data)}
            if not metadata:
                self.store.save_receipt(receipt)
            else:
                self.store.immutable("metadata", runs.receipt_identity(receipt), receipt)
        except Exception:
            # Do not retry. The pre-request cooldown also protects unknown writes.
            self.store.update(state, paper.signed({**state,
                "provider_retry_after": (self.clock()+timedelta(hours=24)).isoformat()}, "state_hash"))
            raise
        self.store.update(state, paper.signed({**state, "provider_retry_after": None}, "state_hash"))
        return receipt

    def pair(self, name, location):
        if name not in self.metadata:
            self.metadata[name] = self.fetch(runs.metadata_url(name), {}, metadata=True)
        selected = runs.select_pair(name, self.metadata[name], self.clock())
        return tuple(self.fetch(ARCHIVE, runs.parameters(name, t, location)) for t in selected)


def grade(store, feeds, clock, report):
    state, now = store.flush(store.load()), clock()
    due = sorted((r["next_label_check"], key) for key,r in state["events"].items()
                 if r["status"] != "settled" and runs.timestamp(r["next_label_check"]) < now)
    for _, key in due[:12]:
        event = store.get("decision", key)
        paper.verify_signed(event, "fingerprint")
        if event["fingerprint"] != state["events"][key]["fingerprint"]:
            raise ValueError("SHADOW_DECISION_JOURNAL_CONFLICT")
        try:
            outcomes, receipts = official_outcomes(event, feeds, clock)
            if outcomes is not None:
                state = store.flush(store.update(state, shadow.settle(state, event, outcomes, receipts, clock())))
                report["graded"] += 1
                continue
        except (requests.RequestException, ValueError) as exc:
            report["errors"].append({"stage": "grade", "id": key, "reason": reason(exc)})
        updated = copy.deepcopy(state)
        updated["events"][key]["next_label_check"] = (now+timedelta(hours=6)).isoformat()
        state = store.update(state, paper.signed(updated, "state_hash"))


def reason(exc):
    return str(exc) if isinstance(exc, ValueError) else type(exc).__name__


def run(store, feeds, *, clock=None, markets=None, forecasts=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    report = {"started_at": started.isoformat(), "captured": 0, "graded": 0, "errors": [], "rejected": {}}
    grade(store, feeds, clock, report)
    state = store.load()
    eligible = []
    for s in shadow.SPEC["stations"]:
        local = started.astimezone(ZoneInfo(station_metadata(s)["timezone"]))
        target = (local.date()+timedelta(days=1)).isoformat()
        if paper.eligible(s, target, started) and any(f"{v}:{s}:{target}" not in state["events"] for v in ("kalshi", "polymarket")):
            eligible.append(s)
    forecasts = forecasts or Forecasts(store, clock)
    if eligible:
        if markets is None:
            markets, errors, catalog_receipts = discover_catalog(feeds)
            report["errors"].extend(errors)
            report["catalog_receipts"] = catalog_receipts
        groups, rejected = defaultdict(list), Counter()
        for raw in markets:
            try:
                c = resolve(raw, raw["_platform"])
                key = f"{c.platform}:{c.station}:{c.target_date}"
                if c.metric != "high" or c.station not in eligible or not paper.eligible(c.station, c.target_date, started):
                    rejected["OUTSIDE_SHADOW_COHORT"] += 1
                elif key not in state["events"]:
                    groups[key].append((c, raw))
            except (ValueError, KeyError, TypeError) as exc:
                rejected[reason(exc)] += 1
        station_inputs = {}
        # Share each exact-run download across venues; all quotes are refreshed
        # after weather acquisition, then validated against the decision clock.
        for key, group in sorted(groups.items()):
            try:
                group.sort(key=lambda x: -float("inf") if x[0].low_f is None else x[0].low_f)
                contracts = [x[0] for x in group]
                c = contracts[0]
                identity = (c.station, c.target_date)
                if identity not in station_inputs:
                    location = store.model["locations"][c.station]
                    baseline_run = runs.timestamp((datetime.fromisoformat(c.target_date).date()-timedelta(days=1)).isoformat()+"T00:00Z")
                    baseline = forecasts.fetch(ARCHIVE, runs.parameters("gfs_global", baseline_run, location))
                    pairs, missing = {}, {}
                    for name in runs.MODEL_DOMAINS:
                        try:
                            pair = forecasts.pair(name, location)
                            runs.verified_pair(*pair, model=name,
                                runs=runs.select_pair(name, forecasts.metadata[name], clock()), station=c.station,
                                target=c.target_date, location=location, decision_at=clock())
                            pairs[name] = pair
                        except (ValueError, requests.RequestException) as exc:
                            if name == "gfs_global":
                                raise
                            missing[name] = reason(exc)
                    station_inputs[identity] = baseline, pairs, missing
                baseline, pairs, missing = station_inputs[identity]
                quotes = [normalized_quote(q) for q in refresh_quotes([x[1] for x in group], c.platform, feeds)]
                if len(quotes) != len(contracts) or any(resolve(q, c.platform) != old for q,old in zip(quotes, contracts)):
                    raise ValueError("SHADOW_CONTRACT_CHANGED")
                event = shadow.decision(store.model, contracts, quotes, baseline, pairs,
                    {name: forecasts.metadata[name] for name in pairs}, missing, clock())
                state = store.flush(store.load())
                store.flush(store.update(state, shadow.reserve_event(state, event)))
                report["captured"] += 1
            except (ValueError, requests.RequestException) as exc:
                # Unknown DB writes or receipt corruption must not trigger a reset.
                store.flush(store.load())
                report["errors"].append({"stage": "capture", "id": key, "reason": reason(exc)})
        report["rejected"] = dict(rejected)
    report.update(shadow.summary(store.load()), completed_at=clock().isoformat(), forecast_downloads=forecasts.downloads)
    expected = {"SHADOW_DOWNLOAD_BUDGET", "SHADOW_PROVIDER_COOLDOWN", "RUN_PROPAGATION_MARGIN"}
    report["status"] = "degraded" if any(e["reason"] not in expected for e in report["errors"]) else "healthy"
    store.output.mkdir(parents=True, exist_ok=True)
    (store.output/"latest.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    store.db.table("app_settings").upsert({"key": shadow.SOURCE+"_latest", "value": report}, on_conflict="key").execute()
    rows = store.db.table("app_settings").select("value").eq("key", shadow.SOURCE+"_latest").execute().data or []
    if len(rows) != 1 or rows[0]["value"] != report:
        raise ValueError("SHADOW_REPORT_NOT_PERSISTED")
    return report
