"""A new, bounded forward paper experiment; never modifies frozen weather models.

One decision per venue/station/target, conservative one-contract simulated fills,
and a durable compare-and-swap journal. No order/account APIs are imported.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scipy.special import ndtr

from backend import weather_adaptive_calibration as adaptive
from backend import weather_residual_calibration as residual
from backend import weather_station_distribution as static
from backend.ml.weather_learning.contracts import station_metadata
from backend.ml.weather_learning.dataset import digest, utc
from backend.weather_historical_calibration import standard_day, finite
from backend.weather_probability_screen import write_once

SOURCE = "weather_daytime_paper_v1"
SPEC = {
    "version": SOURCE, "mode": "paper", "live_ready": False,
    "metric": "high", "stations": list(static.SPEC["stations"]),
    "first_target": "2026-10-23", "last_target": "2026-11-05",
    "decision_hours_local": [8, 11, 14, 17], "lead_days": 1,
    "forecast_model": "gfs_global", "run_policy": "target minus one day, 00 UTC",
    "candidate": "PR20 residual_recenter_only, PR19 Gaussian width",
    "fit_policy": "Fit once before launch; no retraining or station selection during this cohort",
    "min_net_edge": .05, "slippage_per_share": .005, "shares": 1,
    "seed_per_venue": 500., "max_open_risk": 25., "daily_loss_stop": 10.,
    "cumulative_loss_stop": 50., "max_event_cost": 1.,
    "execution": "Simulated one-contract purchase at observed ask plus conservative fee and slippage",
    "independence": "Station/date; venues share one event's weight",
}


def implementation_hash():
    paths = [Path(__file__), Path(__file__).with_name("weather_daytime_collection.py")]
    return digest({"files": {p.name: p.read_text(encoding="utf-8").replace("\r\n", "\n") for p in paths},
                   "parent": residual.implementation_hash()})


def signed(value, field):
    body = {k: v for k, v in value.items() if k != field}
    return {**body, field: digest(body)}


def verify_signed(value, field):
    if value != signed(value, field):
        raise ValueError("DAYTIME_IMMUTABLE_IDENTITY_MISMATCH")


def fit(history_dir, *, clock=None):
    """Archive inputs are available now, never historically backdated as forward."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    directory = Path(history_dir)
    raw, status_raw = (directory / "dataset.json").read_bytes(), (directory / "latest.json").read_bytes()
    status = json.loads(status_raw)
    if status.get("status") not in {"complete", "complete_with_gaps"}:
        raise ValueError("DAYTIME_TRAINING_INCOMPLETE")
    rows = static.validate(json.loads(raw), status["dataset_hash"], now.isoformat())
    cutoff = (date.fromisoformat(SPEC["first_target"]) - timedelta(days=3)).isoformat()
    if max(r["target_date"] for r in rows) > min(cutoff, (now.date() - timedelta(days=1)).isoformat()):
        raise ValueError("DAYTIME_FUTURE_TRAINING_LABEL")
    if now >= utc(SPEC["first_target"] + "T00:00:00Z"):
        raise ValueError("DAYTIME_MODEL_MUST_PRECEDE_COHORT")
    stations = sorted({r["station"] for r in rows})
    if stations != SPEC["stations"]:
        raise ValueError("DAYTIME_STATION_COVERAGE")
    # Reconstruct signed errors from earlier rolling fits, excluding own labels.
    errors = []
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["target_date"]].append(row)
    for target, today in sorted(by_day.items()):
        model = adaptive.fit_day(rows, errors, target, "high", stations)
        if model:
            for row in today:
                mu = row["forecast_high_f"] + model["stations"][row["station"]]["bias_f"]
                errors.append({"station": row["station"], "target_date": target,
                               "label_target_cutoff": model["label_target_cutoff"],
                               "error_f": row["observed_high_f"] - mu})
    base = adaptive.fit_day(rows, errors, SPEC["first_target"], "high", stations)
    shift = residual.residual_model(errors, SPEC["first_target"], "high", stations)
    if not base or not base["uncertainty_ready"]:
        raise ValueError("DAYTIME_FIT_NOT_READY")
    parameters = {s: {"bias_f": base["stations"][s]["bias_f"],
                       "residual_shift_f": shift["stations"][s]["mean_shift_f"],
                       "sigma_f": base["stations"][s]["sigma_f"]} for s in stations}
    locations = {s: status["stations"][s]["station_coordinates"] for s in stations}
    artifact = {"created_at": now.isoformat(), "spec": SPEC, "spec_hash": digest(SPEC),
                "implementation_hash": implementation_hash(), "parameters": parameters,
                "locations": locations, "training_last_target": max(r["target_date"] for r in rows),
                "training_station_dates": len(rows), "training_dataset_hash": digest(rows),
                "input_sha256": hashlib.sha256(raw).hexdigest(), "status_sha256": hashlib.sha256(status_raw).hexdigest(),
                "base_fit_hash": digest(base), "residual_fit_hash": digest(shift),
                "historical_as_of_verified": False, "archive_production_training_eligible": False,
                "experimental_forward_paper_only": True, "live_ready": False}
    return signed(artifact, "model_id")


def verify_model(model):
    verify_signed(model, "model_id")
    if (model["spec"] != SPEC or model["spec_hash"] != digest(SPEC)
            or model["implementation_hash"] != implementation_hash()
            or model["live_ready"] is not False or model["historical_as_of_verified"] is not False
            or set(model["parameters"]) != set(SPEC["stations"])):
        raise ValueError("DAYTIME_MODEL_OR_SPEC_MISMATCH")
    for p in model["parameters"].values():
        if not all(finite(p[k]) for k in ("bias_f", "residual_shift_f", "sigma_f")) or p["sigma_f"] < 1.5:
            raise ValueError("DAYTIME_INVALID_PARAMETERS")


def eligible(station, target, now):
    local = now.astimezone(ZoneInfo(station_metadata(station)["timezone"]))
    return (SPEC["first_target"] <= target <= SPEC["last_target"]
            and local.hour in SPEC["decision_hours_local"]
            and date.fromisoformat(target) == local.date() + timedelta(days=1))


def forecast_high(receipt, station, target, location, now):
    from backend.weather_historical_calibration import ARCHIVE
    params, data = receipt["params"], receipt["data"]
    run = (date.fromisoformat(target) - timedelta(days=1)).isoformat() + "T00:00"
    if (receipt["url"] != ARCHIVE or params.get("run") != run or params.get("models") != "gfs_global"
            or params.get("hourly") != "temperature_2m" or params.get("timezone") != "GMT"
            or any(params.get(k) != location[v] for k, v in (("latitude", "lat"), ("longitude", "lon"), ("elevation", "elev")))
            or digest(data) != receipt["content_hash"] or not utc(run + "Z") <= utc(receipt["received_at"]) <= now):
        raise ValueError("DAYTIME_FORECAST_RECEIPT_MISMATCH")
    if (data.get("hourly_units", {}).get("temperature_2m") != "°F"
            or data.get("utc_offset_seconds") != 0
            or any(not finite(data.get(k)) or abs(data[k] - location[v]) > .5 for k, v in (("latitude", "lat"), ("longitude", "lon")))
            or data.get("elevation") != location["elev"]):
        raise ValueError("DAYTIME_FORECAST_GRID_OR_UNITS")
    times, values = data["hourly"]["time"], data["hourly"]["temperature_2m"]
    if len(times) != len(values) or len(times) != len(set(times)):
        raise ValueError("DAYTIME_FORECAST_TIME_ALIGNMENT")
    start, _ = standard_day(station, date.fromisoformat(target))
    mapping = dict(zip(times, values))
    expected = [int(start.timestamp()) + h * 3600 for h in range(24)]
    if any(not finite(mapping.get(t)) for t in expected):
        raise ValueError("DAYTIME_INCOMPLETE_STANDARD_DAY")
    return max(mapping[t] for t in expected)


def decision(model, contracts, quotes, receipt, now):
    verify_model(model)
    if not contracts or len(contracts) != len(quotes):
        raise ValueError("DAYTIME_INCOMPLETE_VECTOR")
    c = contracts[0]
    if not eligible(c.station, c.target_date, now) or utc(model["created_at"]) >= now:
        raise ValueError("DAYTIME_OUTSIDE_FORWARD_WINDOW")
    start, end = standard_day(c.station, date.fromisoformat(c.target_date))
    if any((x.station, x.target_date, x.platform, x.metric, x.authority) != (c.station, c.target_date, c.platform, "high", c.authority)
           or utc(x.window_start) != start or utc(x.window_end) != end for x in contracts):
        raise ValueError("DAYTIME_CONTRACT_SCOPE")
    if contracts[0].low_f is not None or contracts[-1].high_f is not None or any(
            a.high_f is None or a.high_f != b.low_f for a, b in zip(contracts, contracts[1:])):
        raise ValueError("DAYTIME_INCOMPLETE_PARTITION")
    if len({x.market_id for x in contracts}) != len(contracts):
        raise ValueError("DAYTIME_DUPLICATE_CONTRACT")
    if any(not 0 <= (now - utc(q["received_timestamp"])).total_seconds() <= 180 for q in quotes):
        raise ValueError("DAYTIME_STALE_OR_FUTURE_QUOTES")
    # Require real bid/ask on every bucket; no last-price or synthetic midpoint.
    if any(not all(finite(q.get(k)) for k in ("best_bid", "best_ask")) or
           not 0 <= q["best_bid"] <= q["best_ask"] <= 1 or q["best_ask"] <= 0 for q in quotes):
        raise ValueError("DAYTIME_MISSING_TWO_SIDED_QUOTE")
    raw = forecast_high(receipt, c.station, c.target_date, model["locations"][c.station], now)
    p = model["parameters"][c.station]
    mu, sigma = raw + p["bias_f"] + p["residual_shift_f"], p["sigma_f"]
    probabilities = [float(ndtr(((x.high_f if x.high_f is not None else math.inf) - mu) / sigma)
                           - ndtr(((x.low_f if x.low_f is not None else -math.inf) - mu) / sigma)) for x in contracts]
    if abs(sum(probabilities) - 1) > 1e-10 or any(v < 0 for v in probabilities):
        raise ValueError("DAYTIME_INVALID_PROBABILITIES")
    midpoints = [(q["best_bid"] + q["best_ask"]) / 2 for q in quotes]
    market = [v / sum(midpoints) for v in midpoints]
    from pavlov.pipeline.execution_cost import generate_executable_cost_vector
    costs, depths, cost_errors = [], [], []
    for q in quotes:
        try:
            prices, sizes = generate_executable_cost_vector([q], c.platform, slippage_buffer=SPEC["slippage_per_share"])
            costs.append(prices[0]); depths.append(sizes[0]); cost_errors.append(None)
        except ValueError as exc:
            costs.append(None); depths.append(0); cost_errors.append(str(exc))
    return {"id": f"{c.platform}:{c.station}:{c.target_date}", "source": SOURCE,
            "model_id": model["model_id"], "model_created_at": model["created_at"],
            "decision_at": now.isoformat(), "station": c.station, "target_date": c.target_date,
            "venue": c.platform, "metric": "high", "mode": "paper", "live_ready": False,
            "contracts": [x.record() for x in contracts], "probabilities": probabilities,
            "market_probabilities": market, "quotes": quotes, "costs": costs, "depths": depths,
            "cost_errors": cost_errors, "raw_gfs_high_f": raw, "mean_f": mu, "sigma_f": sigma,
            "forecast_receipt": receipt, "position": None, "paper_execution_enabled": True}


def initial_book(model):
    verify_model(model)
    return signed({"source": SOURCE, "model_id": model["model_id"], "spec_hash": digest(SPEC),
                   "mode": "paper", "live_ready": False, "events": {}, "pending": None,
                   "halted_venues": [], "created_at": datetime.now(timezone.utc).isoformat()}, "state_hash")


def verify_book(book, model):
    verify_signed(book, "state_hash")
    if (book["source"] != SOURCE or book["model_id"] != model["model_id"] or book["spec_hash"] != digest(SPEC)
            or book["mode"] != "paper" or book["live_ready"] is not False):
        raise ValueError("DAYTIME_BOOK_SCOPE_MISMATCH")


def account(book, venue, day):
    events = [r for r in book["events"].values() if r["venue"] == venue]
    settled = [r for r in events if r["status"] == "settled" and r["cost"]]
    pnl = sum(r["pnl"] for r in settled)
    reserved = sum(r["cost"] for r in events if r["status"] != "settled")
    losses = sum(max(0, -r["pnl"]) for r in settled if r["settled_at"][:10] == day)
    return {"realized_paper_pnl": pnl, "reserved": reserved, "daily_losses": losses,
            "available": SPEC["seed_per_venue"] + pnl - reserved,
            "simulated_fills": sum(bool(r["cost"]) for r in events), "settled_fills": len(settled)}


def reserve(book, event):
    if event["id"] in book["events"] or book["pending"]:
        raise ValueError("DAYTIME_DUPLICATE_OR_PENDING_EVENT")
    updated, event = copy.deepcopy(book), copy.deepcopy(event)
    a = account(book, event["venue"], event["decision_at"][:10])
    stopped = (event["venue"] in book["halted_venues"] or a["realized_paper_pnl"] <= -SPEC["cumulative_loss_stop"]
               or a["daily_losses"] >= SPEC["daily_loss_stop"])
    candidates = [(p - cost, -i, i) for i, (p, cost, depth) in enumerate(zip(event["probabilities"], event["costs"], event["depths"]))
                  if cost is not None and 0 < cost < SPEC["max_event_cost"] and depth >= 1
                  and p - cost >= SPEC["min_net_edge"]]
    event["skip_reason"] = "RISK_STOP" if stopped else "NO_EXECUTABLE_EDGE"
    if candidates and not stopped:
        i = max(candidates)[2]
        cost = event["costs"][i]
        # Reserve all open loss exposure against both loss limits, not just cash.
        if (a["reserved"] + cost <= min(SPEC["max_open_risk"], SPEC["daily_loss_stop"] - a["daily_losses"],
                                         SPEC["cumulative_loss_stop"] + a["realized_paper_pnl"])
                and cost <= a["available"]):
            event["position"] = {"bucket_index": i, "shares": 1, "cost": cost,
                                 "fill_kind": "simulated_at_observed_ask_plus_fee_and_slippage"}
            event["skip_reason"] = None
        else:
            event["skip_reason"] = "OPEN_RISK_LIMIT"
    event = signed(event, "fingerprint")
    updated["events"][event["id"]] = {"station": event["station"], "target": event["target_date"],
        "venue": event["venue"], "status": "pending", "cost": (event["position"] or {}).get("cost", 0),
        "fingerprint": event["fingerprint"], "next_label_check": event["contracts"][0]["window_end"]}
    updated["pending"] = {"kind": "decision", "id": event["id"], "record": event}
    return signed(updated, "state_hash")


def settle(book, event, outcomes, receipts, now):
    verify_signed(event, "fingerprint")
    if book["pending"] or book["events"][event["id"]]["status"] == "settled":
        raise ValueError("DAYTIME_SETTLEMENT_ALREADY_PENDING_OR_DONE")
    if (len(outcomes) != len(event["probabilities"]) or any(type(v) is not int or v not in (0, 1) for v in outcomes)
            or sum(outcomes) != 1 or now <= utc(event["contracts"][0]["window_end"])):
        raise ValueError("DAYTIME_OFFICIAL_OUTCOME_VECTOR_INVALID")
    from backend.ml.weather_learning.outcomes import final_result
    if len(receipts) != len(outcomes):
        raise ValueError("DAYTIME_OFFICIAL_RECEIPTS_MISSING")
    for c, y, receipt in zip(event["contracts"], outcomes, receipts):
        venue, market_id = c["platform"], c["market_id"]
        url = ("https://external-api.kalshi.com/trade-api/v2/markets/" + market_id if venue == "kalshi" else
               "https://gateway.polymarket.us/v1/markets/" + market_id + "/settlement")
        market = receipt["data"].get("market", {}) if venue == "kalshi" else receipt["data"]
        if (receipt["url"] != url or digest(receipt["data"]) != receipt["content_hash"]
                or not utc(c["window_end"]) < utc(receipt["received_at"]) <= now
                or final_result(venue, market_id, market) != ("yes" if y else "no")):
            raise ValueError("DAYTIME_OFFICIAL_RECEIPT_MISMATCH")
        if venue == "kalshi":
            rules = "\n".join(str(market.get(k) or "") for k in ("rules_primary", "rules_secondary", "description", "resolution_source")).strip()
            if rules != c["rules_text"]:
                raise ValueError("DAYTIME_FINAL_CONTRACT_CHANGED")
    scores = {name: {"brier": sum((p-y)**2 for p,y in zip(vector, outcomes)),
                     "log_loss": -math.log(max(vector[outcomes.index(1)], 1e-12))}
              for name, vector in (("candidate", event["probabilities"]), ("market", event["market_probabilities"]))}
    position = event["position"]
    pnl = outcomes[position["bucket_index"]] - position["cost"] if position else 0.
    record = signed({"decision_id": event["id"], "decision_fingerprint": event["fingerprint"],
        "outcomes": outcomes, "official_receipts": receipts, "settled_at": now.isoformat(),
        "scores": scores, "paper_pnl": pnl, "real_money_profit": False}, "fingerprint")
    updated = copy.deepcopy(book)
    updated["events"][event["id"]].update(status="settled", pnl=pnl, scores=scores, settled_at=now.isoformat())
    if account(updated, event["venue"], now.date().isoformat())["realized_paper_pnl"] <= -SPEC["cumulative_loss_stop"]:
        updated["halted_venues"] = sorted(set(updated["halted_venues"] + [event["venue"]]))
    updated["pending"] = {"kind": "settlement", "id": event["id"], "record": record}
    return signed(updated, "state_hash")


class Store:
    """Only this source's app_settings keys. Missing books never reset losses."""
    def __init__(self, db, model, output):
        self.db, self.model, self.output = db, model, Path(output)
        verify_model(model)

    def key(self, kind, identity=""):
        return SOURCE + ":" + kind + (":" + identity if identity else "")

    def get(self, kind, identity=""):
        rows = self.db.table("app_settings").select("value").eq("key", self.key(kind, identity)).execute().data or []
        if len(rows) != 1:
            raise ValueError("DAYTIME_SAVED_EVIDENCE_MISSING:" + kind)
        return rows[0]["value"]

    def immutable(self, kind, identity, record):
        self.db.table("app_settings").upsert({"key": self.key(kind, identity), "value": record},
            on_conflict="key", ignore_duplicates=True).execute()
        if self.get(kind, identity) != record:
            raise ValueError("DAYTIME_DATABASE_EVIDENCE_CONFLICT")
        write_once(self.output / kind / (digest(identity) + ".json"), record)

    def load(self):
        book = self.get("book")
        verify_book(book, self.model)
        return book

    def update(self, before, after):
        verify_book(before, self.model); verify_book(after, self.model)
        rows = self.db.table("app_settings").update({"value": after}).eq("key", self.key("book"))\
            .eq("value->>state_hash", before["state_hash"]).execute().data or []
        if len(rows) != 1 or rows[0]["value"] != after:
            raise ValueError("DAYTIME_CONCURRENT_UPDATE_OR_UNKNOWN_WRITE")
        return after

    def flush(self, book):
        if book["pending"]:
            p = book["pending"]
            self.immutable(p["kind"], p["id"], p["record"])
            after = copy.deepcopy(book)
            after["pending"] = None
            book = self.update(book, signed(after, "state_hash"))
        return book


def summary(book):
    by_event = defaultdict(list)
    for r in book["events"].values():
        if r["status"] == "settled":
            by_event[r["station"], r["target"]].append(r["scores"])
    scores = {}
    if by_event:
        for name in ("candidate", "market"):
            scores[name] = {metric: sum(sum(s[name][metric] for s in group) / len(group)
                                      for group in by_event.values()) / len(by_event)
                            for metric in ("brier", "log_loss")}
    return {"source": SOURCE, "mode": "paper", "live_ready": False, "model_id": book["model_id"],
            "decision_vectors": len(book["events"]), "settled_station_dates": len(by_event),
            "settled_target_dates": len({k[1] for k in by_event}), "scores": scores,
            "venues": {v: account(book, v, datetime.now(timezone.utc).date().isoformat()) for v in ("kalshi", "polymarket")},
            "halted_venues": book["halted_venues"], "first_target": SPEC["first_target"], "last_target": SPEC["last_target"],
            "profitability_proven": False, "fill_quality": "simulated; actual execution is untested"}
