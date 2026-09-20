"""Score predictions saved before outcomes, separately from rolling backtests."""
from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict

from . import SOURCE
from .dataset import partition_valid, prepare, utc, vector_key
from .training import normalize, scores


def forward_report(rows, as_of=None):
    current = [r for r in rows if r.get("source") == SOURCE]
    accepted, quality = prepare(current, as_of or datetime.now(timezone.utc))
    models = {}
    if accepted:
        models["market_baseline"] = scores(accepted, normalize(accepted, [r["metadata"]["market_vector_prob"] for r in accepted]))
        models["station_nowcast_prior"] = scores(accepted, normalize(accepted, [r["metadata"]["raw_model_prob"] for r in accepted]))
        for name in ("regularized_logistic", "boosted_trees"):
            groups = defaultdict(list)
            for row in accepted:
                groups[vector_key(row)].append(row)
            learned = [r for group in groups.values() if all(
                name in r["metadata"].get("challenger_probabilities", {})
                and r["metadata"].get("model_trained_at")
                and utc(r["metadata"]["model_trained_at"]) <= utc(r["metadata"]["decision_at"]) for r in group) for r in group]
            if learned:
                models[name] = scores(learned, [r["metadata"]["challenger_probabilities"][name] for r in learned])
                models[name]["matching_market_baseline"] = scores(learned, normalize(learned, [r["metadata"]["market_vector_prob"] for r in learned]))
    return {"mode": "shadow", "live_ready": False, "paper_execution_enabled": False,
            "quality": quality, "models": models,
            "readiness": {"minimum_45_dates": quality["distinct_dates"] >= 45,
                          "minimum_250_station_days": quality["station_days"] >= 250,
                          "executable_profit_verified": False,
                          "promotion": "manual review required; this runner has no execution capability"}}
