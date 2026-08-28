"""Durable, all-candidate prediction evaluation helpers.

Selected-bet tables are intentionally not used as the model-training sample:
choosing the largest apparent edge creates winner's-curse selection bias.  These
rows retain the latest pre-event prediction for every eligible outcome instead.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from loguru import logger

WEATHER_PREDICTION_SOURCE = "weather_calibrated_v2"
MLB_PREDICTION_SOURCE = "mlb_quant_all_games_v2"


def _canonical_team(value: Any) -> str:
    """Conservative team identity used only after an exact game_pk match."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _prediction_game_pk(event_key: Any) -> str | None:
    raw = str(event_key or "").strip()
    if not raw.startswith("mlb:"):
        return None
    game_pk = raw[4:]
    return game_pk if game_pk.isdigit() else None


def resolve_mlb_prediction_backlog(
    db,
    *,
    resolved_at: datetime | None = None,
    row_limit: int = 1000,
    query_chunk_size: int = 100,
) -> dict[str, Any]:
    """Grade unresolved all-game MLB rows from final matches in bounded batches.

    This path is deliberately independent of selected bets and CLV obligations.
    Every exact-game prediction can therefore become training data even when no
    candidate passed the execution gates.
    """
    summary: dict[str, Any] = {
        "unresolved_rows": 0,
        "candidate_events": 0,
        "matched_events": 0,
        "resolved_rows": 0,
        "unmatched_events": 0,
        "winner_mismatch_events": 0,
        "error": None,
    }
    try:
        rows = (
            db.table("model_predictions")
            .select("id,event_key,outcome")
            .eq("source", MLB_PREDICTION_SOURCE)
            .is_("resolved_at", "null")
            .order("created_at")
            .limit(max(1, int(row_limit)))
            .execute()
            .data
            or []
        )
    except Exception as exc:
        summary["error"] = f"prediction_lookup_failed:{exc}"
        logger.warning("MLB prediction backlog lookup failed: {}", exc)
        return summary

    prediction_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        game_pk = _prediction_game_pk(row.get("event_key"))
        if not game_pk or not row.get("id"):
            continue
        prediction_rows.setdefault(game_pk, []).append(row)
    summary["unresolved_rows"] = sum(len(items) for items in prediction_rows.values())
    summary["candidate_events"] = len(prediction_rows)
    if not prediction_rows:
        return summary

    matches: list[dict[str, Any]] = []
    game_pks = sorted(prediction_rows)
    chunk_size = max(1, int(query_chunk_size))
    try:
        for start in range(0, len(game_pks), chunk_size):
            chunk = game_pks[start : start + chunk_size]
            external_ids = [f"mlb_{game_pk}" for game_pk in chunk]
            result = (
                db.table("matches")
                .select("external_id,winner,is_final")
                .eq("sport", "mlb")
                .eq("is_final", True)
                .in_("external_id", external_ids)
                .execute()
            )
            matches.extend(result.data or [])
    except Exception as exc:
        summary["error"] = f"match_lookup_failed:{exc}"
        logger.warning("MLB prediction backlog match lookup failed: {}", exc)
        return summary

    match_by_pk: dict[str, dict[str, Any]] = {}
    for match in matches:
        external_id = str(match.get("external_id") or "")
        game_pk = external_id[4:] if external_id.startswith("mlb_") else external_id
        if game_pk.isdigit() and match.get("is_final") is True and match.get("winner"):
            match_by_pk[game_pk] = match

    correct_ids: list[str] = []
    incorrect_ids: list[str] = []
    for game_pk, event_rows in prediction_rows.items():
        match = match_by_pk.get(game_pk)
        if match is None:
            continue
        winner = _canonical_team(match.get("winner"))
        event_correct: list[str] = []
        event_incorrect: list[str] = []
        for row in event_rows:
            row_id = str(row["id"])
            if winner and _canonical_team(row.get("outcome")) == winner:
                event_correct.append(row_id)
            else:
                event_incorrect.append(row_id)
        # A two-outcome game must have exactly one recognized winner. Fail closed
        # instead of manufacturing an all-false training vector.
        if len(event_correct) != 1:
            summary["winner_mismatch_events"] += 1
            continue
        correct_ids.extend(event_correct)
        incorrect_ids.extend(event_incorrect)
        summary["matched_events"] += 1

    summary["unmatched_events"] = (
        summary["candidate_events"]
        - summary["matched_events"]
        - summary["winner_mismatch_events"]
    )
    if not correct_ids:
        return summary

    stamp = (resolved_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    try:
        for ids, is_correct in ((correct_ids, True), (incorrect_ids, False)):
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start : start + chunk_size]
                if not chunk:
                    continue
                (
                    db.table("model_predictions")
                    .update({"is_correct": is_correct, "resolved_at": stamp})
                    .in_("id", chunk)
                    .execute()
                )
                summary["resolved_rows"] += len(chunk)
    except Exception as exc:
        summary["error"] = f"prediction_update_failed:{exc}"
        logger.warning("MLB prediction backlog update failed: {}", exc)
    return summary


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
        logger.warning("Model evaluation persistence failed for {}: {}", event_key, exc)
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
        logger.warning("MLB evaluation settlement failed for {}: {}", event_key, exc)


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
        logger.warning("Weather evaluation lookup failed for {}: {}", pattern, exc)
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
            logger.warning("Weather evaluation row settlement failed: {}", exc)
    return updated
