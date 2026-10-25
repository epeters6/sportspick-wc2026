"""Prospective, paired forecast-age ablation. No positions or model promotion."""
from __future__ import annotations

import base64
import copy
import gzip
import json
import math
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from scipy.special import ndtr

from backend import weather_daytime_paper as paper
from backend import weather_verified_runs as runs
from backend.ml.weather_learning.dataset import digest
from backend.weather_historical_calibration import ARCHIVE

SOURCE = "weather_verified_run_shadow_v1"
PARENT = "7b7d1c759cf762af36dec1d9789ac7666220631e2b6ecb4fd509785da3da6c18"
SPEC = {"source": SOURCE, "parent_model_id": PARENT, "mode": "shadow", "live_ready": False,
        "paper_execution_enabled": False, "first_target": "2026-10-23", "last_target": "2026-11-05",
        "stations": paper.SPEC["stations"], "decision_hours_local": [8, 11, 14, 17],
        "selection": "first valid prospective slot per venue/station/next-day high",
        "comparison": "same contracts, decision time, market quotes and Gaussian parameters; only GFS run changes",
        "revision_fit": "none; record exact current/previous run differences for later research",
        "guard_weight": 0., "max_downloads_per_invocation": 150,
        "max_downloads_per_utc_day_and_rolling_24h": 150, "provider_cooldown_hours": 24,
        "scores": "multiclass Brier and log loss; venues share station/date weight",
        "candidate_status": "unvalidated transfer of frozen 00UTC correction to newer initialization",
        "publication_claim": "successful response before decision; exact original publication time unknown"}
ARMS = ("market", "frozen_recipe_at_shadow_time", "fresh_gfs_ablation", "guard_market_fallback")


def implementation_hash():
    names = ("weather_verified_runs.py", "weather_verified_shadow.py", "weather_verified_collection.py")
    return digest({n: Path(__file__).with_name(n).read_text(encoding="utf-8").replace("\r\n", "\n") for n in names})


def verify_parent(model):
    paper.verify_model(model)
    if model["model_id"] != PARENT:
        raise ValueError("SHADOW_PARENT_CHANGED")


def initial_state(model, now):
    verify_parent(model)
    if runs.timestamp(now).date().isoformat() > SPEC["last_target"]:
        raise ValueError("SHADOW_COHORT_EXPIRED")
    return paper.signed({"source": SOURCE, "model_id": PARENT, "spec_hash": digest(SPEC),
        "implementation_hash": implementation_hash(), "created_at": now.isoformat(),
        "events": {}, "pending": None, "reservations": [], "provider_retry_after": None,
        "mode": "shadow", "live_ready": False, "paper_execution_enabled": False}, "state_hash")


def verify_state(state):
    paper.verify_signed(state, "state_hash")
    if (state["source"] != SOURCE or state["model_id"] != PARENT or state["spec_hash"] != digest(SPEC)
            or state["implementation_hash"] != implementation_hash() or state["mode"] != "shadow"
            or state["live_ready"] is not False or state["paper_execution_enabled"] is not False):
        raise ValueError("SHADOW_STATE_SCOPE_MISMATCH")


class Store(paper.Store):
    """Separate namespace, immutable compressed receipts and conditional state writes."""
    def __init__(self, db, model, output):
        verify_parent(model)
        super().__init__(db, model, output)

    def key(self, kind, identity=""):
        return SOURCE + ":" + kind + (":" + identity if identity else "")

    def optional(self, kind, identity=""):
        rows = self.db.table("app_settings").select("value").eq("key", self.key(kind, identity)).execute().data or []
        if len(rows) > 1:
            raise ValueError("SHADOW_DUPLICATE_SAVED_KEY")
        return rows[0]["value"] if rows else None

    def load(self):
        value = self.get("state")
        verify_state(value)
        return value

    def update(self, before, after):
        verify_state(before); verify_state(after)
        rows = self.db.table("app_settings").update({"value": after}).eq("key", self.key("state"))\
            .eq("value->>state_hash", before["state_hash"]).execute().data or []
        if len(rows) != 1 or rows[0]["value"] != after:
            raise ValueError("SHADOW_CONCURRENT_UPDATE_OR_UNKNOWN_WRITE")
        return after

    def initialize(self, now):
        self.immutable("spec", digest(SPEC), SPEC)
        self.db.table("app_settings").upsert({"key": self.key("state"), "value": initial_state(self.model, now)},
            on_conflict="key", ignore_duplicates=True).execute()
        return self.load()  # Never resets an existing state or quota ledger.

    def receipt(self, url, params):
        saved = self.optional("receipt", digest({"url": url, "params": params}))
        if saved is None:
            return None
        raw = gzip.decompress(base64.b64decode(saved["gzip_base64"], validate=True))
        record = json.loads(raw)
        if (runs.receipt_identity(record) != saved["receipt_id"] or record["url"] != url or record["params"] != params):
            raise ValueError("SHADOW_RECEIPT_CORRUPT")
        return record

    def save_receipt(self, record):
        identity = digest({"url": record["url"], "params": record["params"]})
        blob = gzip.compress(json.dumps(record, sort_keys=True, allow_nan=False).encode(), mtime=0)
        self.immutable("receipt", identity, {"receipt_id": runs.receipt_identity(record),
                                            "gzip_base64": base64.b64encode(blob).decode()})


def reserve_download(state, identity, now):
    """Charge before sending, including failed/unknown attempts. Keep all reservations."""
    verify_state(state)
    if state["provider_retry_after"] and now < runs.timestamp(state["provider_retry_after"]):
        raise ValueError("SHADOW_PROVIDER_COOLDOWN")
    recent = [r for r in state["reservations"] if runs.timestamp(r["at"]) > now-timedelta(hours=24)]
    if any(runs.timestamp(r["at"]) > now for r in recent):
        raise ValueError("SHADOW_QUOTA_CLOCK_REVERSED")
    daily = [r for r in state["reservations"] if runs.timestamp(r["at"]).date() == now.date()]
    if max(len(recent), len(daily)) >= SPEC["max_downloads_per_utc_day_and_rolling_24h"]:
        raise ValueError("SHADOW_DOWNLOAD_BUDGET")
    updated = copy.deepcopy(state)
    updated["reservations"].append({"at": now.isoformat(), "request_id": identity})
    # A worker killed before recording the response leaves a conservative cooldown.
    updated["provider_retry_after"] = (now+timedelta(hours=24)).isoformat()
    return paper.signed(updated, "state_hash")


def decision(model, contracts, quotes, baseline, pairs, metadata, rejections, now):
    verify_parent(model)
    ref = paper.decision(model, contracts, quotes, baseline, now)
    station, target = ref["station"], ref["target_date"]
    location = model["locations"][station]
    baseline_run = runs.timestamp((date.fromisoformat(target)-timedelta(days=1)).isoformat()+"T00:00Z")
    baseline_info = runs.verify_run(baseline, model="gfs_global", run=baseline_run, station=station,
        target=target, location=location, decision_at=now)
    verified = {}
    for name, (current, previous) in pairs.items():
        selected = runs.select_pair(name, metadata[name], now)
        verified[name] = runs.verified_pair(current, previous, model=name, runs=selected,
            station=station, target=target, location=location, decision_at=now)
    if "gfs_global" not in verified:
        raise ValueError("SHADOW_NO_VERIFIED_GFS_PAIR")
    new = verified["gfs_global"]["current"]
    if runs.timestamp(new["initialization_at"]) <= baseline_run:
        raise ValueError("SHADOW_NO_NEWER_GFS_RUN")
    mean = ref["mean_f"] + new["high_f"] - ref["raw_gfs_high_f"]
    vector = [float(ndtr(((c.high_f if c.high_f is not None else math.inf)-mean)/ref["sigma_f"])
                    - ndtr(((c.low_f if c.low_f is not None else -math.inf)-mean)/ref["sigma_f"])) for c in contracts]
    if any(not runs.finite(p) or not 0 <= p <= 1 for p in vector) or abs(sum(vector)-1) > 1e-10:
        raise ValueError("SHADOW_INVALID_VECTOR")
    vectors = dict(zip(ARMS, (ref["market_probabilities"], ref["probabilities"], vector, ref["market_probabilities"])))
    # Reference arm is recomputed at this shadow time, not an actual paper fill.
    ref.pop("forecast_receipt")
    ref.update(source=SOURCE, mode="shadow", paper_execution_enabled=False, position=None,
        spec_hash=digest(SPEC), implementation_hash=implementation_hash(), vectors=vectors,
        baseline=baseline_info, verified_runs=verified, metadata_receipts=metadata,
        auxiliary_rejections=rejections, fresh_mean_f=mean, candidate_status=SPEC["candidate_status"],
        exact_publication_time_verified=False, guard_weight=0., orders_submitted=False,
        quote_only_net_edges={name: [None if cost is None or depth < 1 else p-cost
            for p,cost,depth in zip(v, ref["costs"], ref["depths"])] for name,v in vectors.items()})
    return paper.signed(ref, "fingerprint")


def reserve_event(state, event):
    verify_state(state); paper.verify_signed(event, "fingerprint")
    if (event["source"] != SOURCE or event["model_id"] != PARENT or event["mode"] != "shadow"
            or event["paper_execution_enabled"] is not False or event["live_ready"] is not False
            or event["position"] is not None or event["spec_hash"] != digest(SPEC)
            or event["implementation_hash"] != implementation_hash()
            or runs.timestamp(event["decision_at"]) < runs.timestamp(state["created_at"])):
        raise ValueError("SHADOW_EVENT_SCOPE")
    if state["pending"] or event["id"] in state["events"]:
        raise ValueError("SHADOW_DUPLICATE_OR_PENDING")
    updated = copy.deepcopy(state)
    updated["events"][event["id"]] = {"station": event["station"], "target": event["target_date"],
        "venue": event["venue"], "status": "pending", "fingerprint": event["fingerprint"],
        "next_label_check": event["contracts"][0]["window_end"]}
    updated["pending"] = {"kind": "decision", "id": event["id"], "record": event}
    return paper.signed(updated, "state_hash")


def settle(state, event, outcomes, receipts, now):
    verify_state(state); paper.verify_signed(event, "fingerprint")
    saved = state["events"][event["id"]]
    if state["pending"] or saved["status"] == "settled" or saved["fingerprint"] != event["fingerprint"]:
        raise ValueError("SHADOW_SETTLEMENT_CONFLICT")
    # Reuse the frozen strict official-label validator in memory only. No paper
    # store/account is read or written and this event has no position.
    validation_book = {"events": {event["id"]: {**saved, "cost": 0}}, "pending": None, "halted_venues": []}
    paper.settle(validation_book, event, outcomes, receipts, now)
    scores = {name: {"brier": sum((p-y)**2 for p,y in zip(vector, outcomes)),
        "log_loss": -math.log(max(vector[outcomes.index(1)], 1e-12))} for name,vector in event["vectors"].items()}
    record = paper.signed({"source": SOURCE, "decision_fingerprint": event["fingerprint"],
        "decision_id": event["id"], "settled_at": now.isoformat(), "outcomes": outcomes,
        "official_receipts": receipts, "scores": scores, "realized_profit": None,
        "live_ready": False, "paper_execution_enabled": False}, "fingerprint")
    updated = copy.deepcopy(state)
    updated["events"][event["id"]].update(status="settled", scores=scores)
    updated["pending"] = {"kind": "settlement", "id": event["id"], "record": record}
    return paper.signed(updated, "state_hash")


def summary(state):
    grouped = defaultdict(list)
    for e in state["events"].values():
        if e["status"] == "settled":
            grouped[e["station"], e["target"]].append(e["scores"])
    scores = {name: {metric: sum(sum(s[name][metric] for s in group)/len(group)
        for group in grouped.values())/len(grouped) for metric in ("brier", "log_loss")} for name in ARMS} if grouped else {}
    comparisons = {}
    for arm in ("frozen_recipe_at_shadow_time", "fresh_gfs_ablation"):
        by_date = defaultdict(list)
        for (_, target), venues in grouped.items():
            by_date[target].append(sum(v[arm]["brier"]-v["market"]["brier"] for v in venues)/len(venues))
        blocks = [by_date[day] for day in sorted(by_date)]
        interval = None
        if len(blocks) >= 7:
            # Preserve dependence across stations sharing a weather date. Venues
            # have already been averaged inside each station/date event.
            rng, samples = random.Random(0), []
            for _ in range(2000):
                sampled = [blocks[rng.randrange(len(blocks))] for _ in blocks]
                samples.append(sum(sum(b) for b in sampled)/sum(len(b) for b in sampled))
            samples.sort()
            interval = [samples[49], samples[1949]]
        comparisons[arm] = {"brier_minus_market": scores[arm]["brier"]-scores["market"]["brier"] if scores else None,
            "date_block_bootstrap_95_interval": interval, "interval_minimum_target_dates": 7,
            "interpretation": "negative favors candidate; exploratory, no promotion or profitability inference"}
    return {"source": SOURCE, "mode": "shadow", "live_ready": False, "paper_execution_enabled": False,
        "model_id": PARENT, "decision_vectors": len(state["events"]), "settled_station_dates": len(grouped),
        "settled_target_dates": len({key[1] for key in grouped}), "scores": scores, "paired_comparisons": comparisons,
        "spec_hash": digest(SPEC), "implementation_hash": implementation_hash(),
        "guard_weight": 0., "profitability_proven": False, "realized_profit": None,
        "provider_retry_after": state["provider_retry_after"]}
