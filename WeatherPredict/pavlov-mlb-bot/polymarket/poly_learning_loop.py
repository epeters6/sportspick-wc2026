"""Post-resolution learning for Polymarket US positions (isolated from Kalshi)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import pipeline.calibration_log as calibration_log
import pipeline.ensemble_client as ensemble_client
import pipeline.signal_learning_log as signal_learning_log
from pipeline.learning_loop import _fetch_actual_temp  # METAR history helper

from polymarket import paths as poly_paths
from polymarket import poly_client

logger = logging.getLogger(__name__)

_SCORE_WIN_MULT  = 1.05
_SCORE_LOSS_MULT = 0.90
_SCORE_MIN       = 0.50
_SCORE_MAX       = 1.50
_WATCH_WIN_MULT  = 1.02
_WATCH_LOSS_MULT = 0.98


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def check_and_resolve() -> list[dict]:
    """Resolve open Polymarket positions using settlement API."""
    with ensemble_client.isolated_storage(poly_paths.ENSEMBLE_CACHE, poly_paths.ENSEMBLE_BIAS):
        with calibration_log.use_forecast_errors_file(poly_paths.FORECAST_ERRORS):
            resolved = _check_and_resolve_core()
            # Skip-bias updates must run before exiting isolated_storage so bias
            # writes hit logs_poly/ensemble_bias.json, not Kalshi paths.
            _process_skip_bias_core()
            return resolved


def _check_and_resolve_core() -> list[dict]:
    positions: list[dict] = _load_json(poly_paths.POSITIONS, [])
    scores: dict[str, float] = _load_json(poly_paths.STATION_SCORES, {})
    resolved: list[dict] = []

    for pos in positions:
        if pos.get("status") != "open":
            continue
        slug = pos.get("ticker", "")
        try:
            result = poly_client.get_market_result(slug)
        except Exception as exc:
            logger.warning("PolyLearning: result check %s — %s", slug, exc)
            continue
        if result is None:
            continue

        won: bool = result == pos.get("recommended_side")
        price_cents: int = int(pos.get("price_cents") or 50)
        contracts: int = int(pos.get("kelly_contracts") or 1)

        if won:
            pl = (1 - price_cents / 100) * contracts
            pos["status"] = "won"
        else:
            pl = -(price_cents / 100) * contracts
            pos["status"] = "lost"

        pos["pl"] = round(pl, 4)
        pos["resolved_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "PolyLearning: %s resolved → %s P&L=$%.2f",
            slug, pos["status"], pl,
        )

        city = pos.get("city", "")
        if city:
            current = float(scores.get(city, 1.0))
            mult = _SCORE_WIN_MULT if won else _SCORE_LOSS_MULT
            updated = max(_SCORE_MIN, min(_SCORE_MAX, current * mult))
            scores[city] = round(updated, 4)

        station_id  = pos.get("station", "")
        market_date = pos.get("market_date", "")
        metric      = pos.get("metric", "high")

        actual_val: Optional[float] = None
        if not pos.get("actual_temp_f") and station_id and market_date:
            actual_val = _fetch_actual_temp(station_id, market_date, metric)
            if actual_val is not None:
                pos["actual_temp_f"] = actual_val
        else:
            actual_val = pos.get("actual_temp_f")

        ens_mean = pos.get("ensemble_mean")
        if ens_mean is not None and actual_val is not None and city:
            error_f = float(ens_mean) - float(actual_val)
            ensemble_client.update_bias(city, metric, error_f)

        if actual_val is not None and city and market_date:
            calibration_log.record_resolution(
                city            = city,
                metric          = metric,
                date_str        = market_date,
                actual_temp_f   = float(actual_val),
                ensemble_mean   = pos.get("ensemble_mean"),
                ensemble_spread = pos.get("ensemble_spread"),
                nws_predicted   = pos.get("nws_predicted"),
                owm_predicted   = pos.get("owm_predicted"),
            )

        resolved.append(pos)

    if resolved:
        _save_json(poly_paths.POSITIONS, positions)
        _save_json(poly_paths.STATION_SCORES, scores)
        logger.info("PolyLearning: resolved %d positions.", len(resolved))

    return resolved


def _process_skip_bias_core() -> None:
    signals: list[dict] = _load_json(poly_paths.SIGNALS, [])
    scores: dict[str, float] = _load_json(poly_paths.STATION_SCORES, {})
    signals_dirty = False
    scores_dirty = False
    for sig in signals:
        if sig.get("action") not in signal_learning_log.LEARNING_ACTIONS:
            continue
        if sig.get("bias_updated"):
            continue

        station_id  = sig.get("station", "")
        market_date = sig.get("market_date", "")
        metric      = sig.get("metric", "high")
        city        = sig.get("city", "")
        ens_mean    = sig.get("ensemble_mean")

        if not (station_id and market_date and city and ens_mean is not None):
            continue
        try:
            mkt_dt = datetime.strptime(market_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if mkt_dt.date() >= datetime.now(timezone.utc).date():
                continue
        except ValueError:
            continue

        if not sig.get("actual_temp_f"):
            actual_val = _fetch_actual_temp(station_id, market_date, metric)
            if actual_val is not None:
                sig["actual_temp_f"] = actual_val
        else:
            actual_val = sig.get("actual_temp_f")

        if actual_val is not None:
            error_f = float(ens_mean) - float(actual_val)
            ensemble_client.update_bias(city, metric, error_f)
            sig["bias_updated"] = True
            signals_dirty = True
            calibration_log.record_resolution(
                city            = city,
                metric          = metric,
                date_str        = market_date,
                actual_temp_f   = float(actual_val),
                ensemble_mean   = sig.get("ensemble_mean"),
                ensemble_spread = sig.get("ensemble_spread"),
                nws_predicted   = sig.get("nws_predicted"),
                owm_predicted   = sig.get("owm_predicted"),
            )

            direction    = sig.get("direction", "")
            threshold    = sig.get("threshold_f")
            threshold_lo = sig.get("threshold_lo")
            threshold_hi = sig.get("threshold_hi")
            actual_yes: bool | None = None
            if direction == "above" and threshold is not None:
                actual_yes = float(actual_val) > float(threshold)
            elif direction == "below" and threshold is not None:
                actual_yes = float(actual_val) < float(threshold)
            elif (
                direction == "in_range"
                and threshold_lo is not None
                and threshold_hi is not None
            ):
                actual_yes = float(threshold_lo) <= float(actual_val) <= float(threshold_hi)

            would_won: bool | None = None
            if actual_yes is not None:
                rec = str(sig.get("recommended_side", "")).lower()
                if rec not in ("yes", "no"):
                    model_prob = float(sig.get("model_prob", 0.5))
                    implied_prob = float(sig.get("implied_prob", 0.5))
                    side_yes = model_prob > implied_prob
                else:
                    side_yes = rec == "yes"
                would_won = (side_yes and actual_yes) or ((not side_yes) and (not actual_yes))
                sig["actual_yes"] = actual_yes
                sig["would_have_won"] = would_won

            if not sig.get("station_nudged") and would_won is not None and city:
                current = float(scores.get(city, 1.0))
                mult = _WATCH_WIN_MULT if would_won else _WATCH_LOSS_MULT
                upd = max(_SCORE_MIN, min(_SCORE_MAX, current * mult))
                scores[city] = round(upd, 4)
                sig["station_nudged"] = True
                scores_dirty = True

            logger.info(
                "PolyLearning: skip/watch bias for %s %s — error=%+.1f°F would_have_won=%s",
                city,
                sig.get("ticker", ""),
                error_f,
                sig.get("would_have_won", "—"),
            )

    if signals_dirty:
        _save_json(poly_paths.SIGNALS, signals)
    if scores_dirty:
        _save_json(poly_paths.STATION_SCORES, scores)


def generate_summary(hours: int = 24) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    positions: list[dict] = _load_json(poly_paths.POSITIONS, [])
    signals: list[dict]   = _load_json(poly_paths.SIGNALS, [])

    recent = []
    for pos in positions:
        resolved_str = pos.get("resolved_at")
        if not resolved_str:
            continue
        try:
            resolved_at = datetime.fromisoformat(resolved_str)
            if resolved_at.tzinfo is None:
                resolved_at = resolved_at.replace(tzinfo=timezone.utc)
            if resolved_at >= cutoff:
                recent.append(pos)
        except (ValueError, TypeError):
            continue

    pl_total = sum(p.get("pl", 0) or 0 for p in recent)
    wins     = sum(1 for p in recent if p.get("status") == "won")
    losses   = sum(1 for p in recent if p.get("status") == "lost")
    total    = wins + losses
    win_rate = wins / total if total else 0.0

    city_wins: dict[str, int] = {}
    for p in recent:
        if p.get("status") == "won":
            c = p.get("city", "")
            city_wins[c] = city_wins.get(c, 0) + 1
    best_city = max(city_wins, key=lambda c: city_wins[c]) if city_wins else "—"

    signals_fired = 0
    for sig in signals:
        ts_str = sig.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                signals_fired += 1
        except (ValueError, TypeError):
            continue
    for pos in positions:
        placed_str = pos.get("placed_at", "")
        try:
            placed = datetime.fromisoformat(placed_str)
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=timezone.utc)
            if placed >= cutoff:
                signals_fired += 1
        except (ValueError, TypeError):
            continue

    bankroll = 0.0
    try:
        bankroll = poly_client.get_account_balance()
    except Exception:
        pass

    return {
        "pl":            round(pl_total, 2),
        "wins":          wins,
        "losses":        losses,
        "total":         total,
        "win_rate":      round(win_rate, 4),
        "bankroll":      round(bankroll, 2),
        "signals_fired": signals_fired,
        "best_city":     best_city,
    }
