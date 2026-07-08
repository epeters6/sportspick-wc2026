"""Order placement + skip logging for Polymarket US (isolated from Kalshi)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone

from config import CONFIG
from pipeline import signal_learning_log
from polymarket import paths as poly_paths
from polymarket import poly_client

logger = logging.getLogger(__name__)


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


async def place_trade(signal: dict) -> dict:
    """Place a limit order on Polymarket US."""
    slug      = signal["ticker"]
    side      = signal["recommended_side"]
    contracts = int(signal["kelly_contracts"])

    # YES: limit price ≈ cost per $1 payoff share of YES.
    # NO: unit here is cost of NO (1 − YES implied); Polymarket ``BUY_SHORT`` uses the
    # complementary YES price inside ``poly_client.place_order``.
    raw_price = (
        signal["implied_prob"] if side == "yes"
        else (1 - signal["implied_prob"])
    )
    price_prob = max(0.01, min(0.99, float(raw_price)))

    # Whole-dollar notional floor: est. spend = qty × unit; round up to \$1 steps (min POLY_MIN_NOTIONAL_USD).
    min_usd = float(CONFIG.get("POLY_MIN_NOTIONAL_USD") or 0.0)
    if min_usd > 0 and contracts >= 1:
        raw_stake_usd = contracts * price_prob
        target_usd = max(min_usd, math.ceil(raw_stake_usd - 1e-9))
        need = max(contracts, int(math.ceil(target_usd / price_prob - 1e-9)))
        if need > contracts:
            logger.info(
                "PolyOrderManager: %s qty %d → %d (est. ~$%.2f → ceil whole USD $%.0f; unit=%.3f)",
                slug,
                contracts,
                need,
                raw_stake_usd,
                target_usd,
                price_prob,
            )
            contracts = need

    try:
        result = await asyncio.to_thread(
            poly_client.place_order,
            slug,
            side,
            contracts,
            price_prob,
        )
    except Exception as exc:
        logger.error("PolyOrderManager: place_order raised – %s", exc)
        return {"success": False, "error": str(exc)}

    if result.get("status") == "error" or not result.get("order_id"):
        err = result.get("error", "unknown error")
        logger.error("PolyOrderManager: order failed for %s – %s", slug, err)
        return {"success": False, "error": err}

    fill_px = result.get("price")
    try:
        price_cents = int(round(float(fill_px) * 100)) if fill_px is not None else int(
            round(price_prob * 100)
        )
    except (TypeError, ValueError):
        price_cents = int(round(price_prob * 100))

    positions = _load_json(poly_paths.POSITIONS, [])
    positions.append(
        {
            "venue":            "poly_us",
            "order_id":         result["order_id"],
            "ticker":           slug,
            "city":             signal.get("city", ""),
            "metric":           signal.get("metric", ""),
            "direction":        signal.get("direction", ""),
            "threshold_f":      signal.get("threshold_f"),
            "market_date":      signal.get("market_date", ""),
            "nws_predicted":    signal.get("nws_predicted"),
            "ensemble_mean":    signal.get("ensemble_mean"),
            "ensemble_spread":  signal.get("ensemble_spread"),
            "days_out":         signal.get("days_out", 0),
            "station":          signal.get("station", ""),
            "recommended_side": side,
            "kelly_contracts":  contracts,
            "price_cents":      price_cents,
            "placed_at":        datetime.now(timezone.utc).isoformat(),
            "status":           "open",
            "edge":             signal.get("edge"),
            "model_prob":       signal.get("model_prob"),
            "placed_via":       signal.get("placed_via", "manual"),
            "resolved_at":      None,
            "actual_temp_f":    None,
            "pl":               None,
        }
    )
    _save_json(poly_paths.POSITIONS, positions)

    logger.info(
        "PolyOrderManager: position opened – %s %s %d @ ~%d¢ order=%s",
        side.upper(), slug, contracts, price_cents, result["order_id"],
    )
    return {"success": True, "order_id": result["order_id"]}


async def place_mlb_trade(signal: dict) -> dict:
    """Place a Polymarket order for an MLB moneyline signal; logs ``mlb_poly`` position."""
    slug = signal["ticker"]
    side = signal["recommended_side"]
    contracts = int(signal["kelly_contracts"])
    raw_price = (
        signal["implied_prob"] if side == "yes" else (1 - signal["implied_prob"])
    )
    price_prob = max(0.01, min(0.99, float(raw_price)))

    min_usd = float(CONFIG.get("POLY_MIN_NOTIONAL_USD") or 0.0)
    if min_usd > 0 and contracts >= 1:
        raw_stake_usd = contracts * price_prob
        target_usd = max(min_usd, math.ceil(raw_stake_usd - 1e-9))
        need = max(contracts, int(math.ceil(target_usd / price_prob - 1e-9)))
        if need > contracts:
            contracts = need

    try:
        result = await asyncio.to_thread(
            poly_client.place_order,
            slug,
            side,
            contracts,
            price_prob,
        )
    except Exception as exc:
        logger.error("PolyOrderManager: MLB place_order raised – %s", exc)
        return {"success": False, "error": str(exc)}

    if result.get("status") == "error" or not result.get("order_id"):
        err = result.get("error", "unknown error")
        logger.error("PolyOrderManager: MLB order failed for %s – %s", slug, err)
        return {"success": False, "error": err}

    fill_px = result.get("price")
    try:
        price_cents = int(round(float(fill_px) * 100)) if fill_px is not None else int(
            round(price_prob * 100)
        )
    except (TypeError, ValueError):
        price_cents = int(round(price_prob * 100))

    positions = _load_json(poly_paths.POSITIONS, [])
    positions.append(
        {
            "venue": "mlb_poly",
            "order_id": result["order_id"],
            "ticker": slug,
            "game_id": signal.get("game_id"),
            "home_team_abbr": signal.get("home_team_abbr", ""),
            "away_team_abbr": signal.get("away_team_abbr", ""),
            "home_pitcher_id": signal.get("home_pitcher_id"),
            "away_pitcher_id": signal.get("away_pitcher_id"),
            "yes_is_home": signal.get("yes_is_home"),
            "model_home_prob": signal.get("model_home_prob"),
            "predicted_home_win": signal.get("predicted_home_win"),
            "park_run_factor": signal.get("park_run_factor"),
            "edge": signal.get("edge"),
            "focus_pitcher_name": signal.get("focus_pitcher_name"),
            "recommended_side": side,
            "kelly_contracts": contracts,
            "price_cents": price_cents,
            "placed_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "placed_via": signal.get("placed_via", "manual"),
            "resolved_at": None,
            "pl": None,
        }
    )
    _save_json(poly_paths.POSITIONS, positions)
    logger.info(
        "PolyOrderManager: MLB position – %s %s game=%s contracts=%d @ ~%d¢",
        side.upper(),
        slug,
        signal.get("game_id"),
        contracts,
        price_cents,
    )
    return {"success": True, "order_id": result["order_id"]}


def log_skip(signal: dict) -> None:
    """Append a skip record to logs_poly/signals.json."""
    if signal_learning_log.append_learning_record(
        poly_paths.SIGNALS,
        signal,
        action="skip",
        learn_reason="discord_skip",
        learn_source="discord",
        venue="poly_us",
    ):
        logger.info("PolyOrderManager: skip logged for %s.", signal.get("ticker"))


def log_mlb_skip(signal: dict) -> None:
    """Log a Discord skip for an MLB Polymarket signal (``venue`` = ``mlb_poly``)."""
    extra = {
        "game_id": signal.get("game_id"),
        "home_pitcher_id": signal.get("home_pitcher_id"),
        "away_pitcher_id": signal.get("away_pitcher_id"),
        "home_team_abbr": signal.get("home_team_abbr"),
        "away_team_abbr": signal.get("away_team_abbr"),
        "model_home_prob": signal.get("model_home_prob"),
        "yes_is_home": signal.get("yes_is_home"),
        "predicted_home_win": signal.get("predicted_home_win"),
        "park_run_factor": signal.get("park_run_factor"),
        "venue_name": signal.get("venue_name"),
    }
    if signal_learning_log.append_learning_record(
        poly_paths.SIGNALS,
        signal,
        action="skip",
        learn_reason="discord_mlb_skip",
        learn_source="discord",
        venue="mlb_poly",
        extra_fields=extra,
    ):
        logger.info("PolyOrderManager: MLB skip logged for %s.", signal.get("ticker"))


def log_signal_watch(signal: dict, reason: str) -> None:
    """Log a Poly signal that did not become a position (e.g. auto-bet rejected)."""
    if signal_learning_log.append_learning_record(
        poly_paths.SIGNALS,
        signal,
        action="signal_watch",
        learn_reason=(reason or "").strip(),
        learn_source="auto",
        venue="poly_us",
    ):
        logger.info(
            "PolyOrderManager: signal_watch logged for %s — %s",
            signal.get("ticker"),
            (reason or "")[:160],
        )
