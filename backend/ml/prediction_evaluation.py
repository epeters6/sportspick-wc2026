"""Durable, all-candidate prediction evaluation helpers.

Selected-bet tables are intentionally not used as the model-training sample:
choosing the largest apparent edge creates winner's-curse selection bias.  These
rows retain the latest pre-event prediction for every eligible outcome instead.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from loguru import logger

WEATHER_PREDICTION_SOURCE = "weather_calibrated_v2"
MLB_PREDICTION_SOURCE = "mlb_quant_all_games_v2"


def replace_prediction_rows(
    db,
    *,
    source: str,
    domain: str,
    event_key: str,
    rows: list[dict[str, Any]],
) -> int:
    """Replace one event's prediction vector, keeping the latest pre-event view."""
    payload: list[dict[str, Any]] = []
    created_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        try:
            probability = float(row["prob"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            continue
        item = {
            "source": source,
            "domain": domain,
            "event_key": event_key,
            "outcome": str(row["outcome"]),
            "prob": probability,
            "market_price": row.get("market_price"),
            "edge": row.get("edge"),
            "metadata": row.get("metadata") or {},
            "created_at": created_at,
            "resolved_at": None,
            "is_correct": None,
        }
        payload.append(item)
    if not payload:
        return 0
    try:
        (
            db.table("model_predictions")
            .delete()
            .eq("source", source)
            .eq("event_key", event_key)
            .execute()
        )
        db.table("model_predictions").insert(payload).execute()
        return len(payload)
    except Exception as exc:
        # Prediction persistence must not fabricate a bet or crash settlement.
        logger.warning("Model evaluation persistence failed for %s: %s", event_key, exc)
        return 0


def resolve_mlb_prediction_rows(
    db,
    *,
    game_pk: str | int,
    winner: str,
    resolved_at: datetime,
) -> None:
    """Resolve both sides of an all-game MLB prediction by exact game_pk."""
    event_key = f"mlb:{game_pk}"
    stamp = resolved_at.astimezone(timezone.utc).isoformat()
    try:
        (
            db.table("model_predictions")
            .update({"is_correct": False, "resolved_at": stamp})
            .eq("source", MLB_PREDICTION_SOURCE)
            .eq("event_key", event_key)
            .execute()
        )
        (
            db.table("model_predictions")
            .update({"is_correct": True, "resolved_at": stamp})
            .eq("source", MLB_PREDICTION_SOURCE)
            .eq("event_key", event_key)
            .eq("outcome", winner)
            .execute()
        )
    except Exception as exc:
        logger.warning("MLB evaluation settlement failed for %s: %s", event_key, exc)


def _in_bucket(actual: float, low: Any, high: Any) -> bool:
    lo = float("-inf") if low is None else float(low)
    hi = float("inf") if high is None else float(high)
    return lo <= float(actual) < hi


def resolve_weather_prediction_rows(
    db,
    *,
    station: str,
    target_date: str,
    metric: str,
    actual: float,
    resolved_at: datetime | None = None,
) -> int:
    """Resolve every venue/bucket for a verified station/metric/date."""
    pattern = f"weather:%:{station}:{target_date}:{metric}"
    try:
        rows = (
            db.table("model_predictions")
            .select("id,metadata")
            .eq("source", WEATHER_PREDICTION_SOURCE)
            .like("event_key", pattern)
            .is_("resolved_at", "null")
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning("Weather evaluation lookup failed for %s: %s", pattern, exc)
        return 0

    stamp = (resolved_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    updated = 0
    for row in rows:
        meta = row.get("metadata") or {}
        try:
            correct = _in_bucket(
                actual, meta.get("bucket_low_f"), meta.get("bucket_high_f")
            )
            (
                db.table("model_predictions")
                .update({"is_correct": correct, "resolved_at": stamp})
                .eq("id", row["id"])
                .execute()
            )
            updated += 1
        except Exception as exc:
            logger.warning("Weather evaluation row settlement failed: %s", exc)
    return updated
