"""
pipeline/learning_loop.py – Post-resolution scoring and daily summary.

Public API
----------
check_and_resolve(kalshi_client) -> list[dict]
generate_summary(hours=24)       -> dict
check_mlb_resolutions(mlb_module, polymarket_client=None) -> list[dict]
generate_mlb_daily_summary(hours=24) -> dict
generate_daily_summary           -> dict  (MLB 24h alias)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import pipeline.ensemble_client as ensemble_client
import pipeline.calibration_log as calibration_log
import pipeline.signal_learning_log as signal_learning_log
import data_paths as dp
from polymarket import poly_client

logger = logging.getLogger(__name__)

_POSITIONS_FILE = os.path.join(dp.logs_dir(), "positions.json")
_SCORES_FILE    = os.path.join(dp.logs_dir(), "team_scores.json")
_SIGNALS_FILE   = os.path.join(dp.logs_dir(), "signals.json")

_SCORE_WIN_MULT  = 1.05
_SCORE_LOSS_MULT = 0.90
_SCORE_MIN       = 0.50
_SCORE_MAX       = 1.50
# Softer nudge for skip / signal_watch outcomes (no capital at risk).
_WATCH_WIN_MULT  = 1.02
_WATCH_LOSS_MULT = 0.98

_METAR_BASE = "https://aviationweather.gov/api/data/metar"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Actual temperature fetcher (METAR history)
# ---------------------------------------------------------------------------

def _fetch_actual_temp(
    station_id: str,
    market_date: str,
    metric: str,
) -> Optional[float]:
    """Fetch the observed high or low temperature for a station on a past date.

    Queries the Aviation Weather Center METAR archive for the target date and
    returns the max (high) or min (low) observed 2-m temperature in °F.

    Args:
        station_id:  ICAO code, e.g. 'KJFK'.
        market_date: YYYY-MM-DD string of the measurement date.
        metric:      'high' or 'low'.

    Returns:
        Temperature in °F, or None if data is unavailable.
    """
    try:
        target_dt = datetime.strptime(market_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    now_utc   = datetime.now(timezone.utc)
    hours_ago = int((now_utc - target_dt).total_seconds() / 3600)

    # We want observations from the full target date.  Ask for enough hours
    # to cover the date plus a one-day buffer on each side, capped at 96 h
    # (the maximum the Aviation Weather API typically honours).
    hours_back = min(96, max(24, hours_ago + 24))

    url = (
        f"{_METAR_BASE}?ids={station_id}&format=json"
        f"&taf=false&hours={hours_back}"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug(
            "LearningLoop: METAR history fetch failed for %s on %s – %s",
            station_id, market_date, exc,
        )
        return None

    if not data or not isinstance(data, list):
        return None

    # Collect all temperature readings for the target calendar date (UTC).
    # US highs typically occur in the afternoon local time, which for eastern
    # cities is roughly 19–23 UTC.  Using the full UTC calendar date is a
    # good-enough proxy for the daily extreme.
    target_date = target_dt.date()
    day_temps: list[float] = []
    for obs in data:
        report_time_str = obs.get("reportTime", "")
        temp_c = obs.get("temp")
        if temp_c is None or not report_time_str:
            continue
        try:
            obs_dt = datetime.fromisoformat(report_time_str.replace("Z", "+00:00"))
            if obs_dt.date() == target_date:
                day_temps.append(float(temp_c) * 9 / 5 + 32)
        except (ValueError, TypeError):
            continue

    if not day_temps:
        logger.debug(
            "LearningLoop: no METAR observations for %s on %s (checked %d obs).",
            station_id, market_date, len(data),
        )
        return None

    actual = max(day_temps) if metric == "high" else min(day_temps)
    logger.info(
        "LearningLoop: actual %s for %s on %s = %.1f°F (%d obs).",
        metric, station_id, market_date, actual, len(day_temps),
    )
    return round(actual, 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_and_resolve(kalshi_client) -> list[dict]:
    """Poll Kalshi for results on all open positions and update scoring.

    For every open position:
        1. Calls ``kalshi_client.get_market_result(ticker)``.
        2. Skips if the market is not yet settled (returns None).
        3. Computes P&L and marks the position as 'won' or 'lost'.
        4. Multiplies the city's station score by 1.05 (win) or 0.90 (loss),
           clamped to [0.50, 1.50].

    Saves updated positions.json and station_scores.json.

    Returns:
        List of position dicts that were resolved in this call.
    """
    positions: list[dict] = _load_json(_POSITIONS_FILE, [])
    scores: dict[str, float] = _load_json(_SCORES_FILE, {})

    resolved: list[dict] = []

    for pos in positions:
        if pos.get("status") != "open":
            continue

        ticker = pos.get("ticker", "")
        try:
            result = kalshi_client.get_market_result(ticker)
        except Exception as exc:
            logger.warning(
                "LearningLoop: error checking result for %s – %s", ticker, exc
            )
            continue

        if result is None:
            # Market not yet settled.
            continue

        # ── Determine outcome ─────────────────────────────────────────
        won: bool = result == pos.get("recommended_side")
        price_cents: int = pos.get("price_cents", 50)
        contracts: int   = pos.get("kelly_contracts", 1)

        if won:
            pl = (1 - price_cents / 100) * contracts
            pos["status"] = "won"
        else:
            pl = -(price_cents / 100) * contracts
            pos["status"] = "lost"

        pos["pl"]          = round(pl, 4)
        pos["resolved_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "LearningLoop: %s resolved → %s  P&L=$%.2f",
            ticker, pos["status"], pl,
        )

        # ── Update station score ──────────────────────────────────────
        city = pos.get("city", "")
        if city:
            current = float(scores.get(city, 1.0))
            multiplier = _SCORE_WIN_MULT if won else _SCORE_LOSS_MULT
            updated = max(_SCORE_MIN, min(_SCORE_MAX, current * multiplier))
            scores[city] = round(updated, 4)
            logger.info(
                "LearningLoop: %s score %.4f → %.4f (%s)",
                city, current, updated, "WIN" if won else "LOSS",
            )

        # ── Fetch actual temperature and update ensemble bias ─────────
        # Attempt to populate actual_temp_f from METAR history so the
        # ensemble bias correction EMA has real data to learn from.
        station_id  = pos.get("station", "")
        market_date = pos.get("market_date", "")
        metric      = pos.get("metric", "high")

        if not pos.get("actual_temp_f") and station_id and market_date:
            actual_val = _fetch_actual_temp(station_id, market_date, metric)
            if actual_val is not None:
                pos["actual_temp_f"] = actual_val
        else:
            actual_val = pos.get("actual_temp_f")

        ens_mean = pos.get("ensemble_mean")
        if ens_mean is not None and actual_val is not None and city:
            error_f = float(ens_mean) - float(actual_val)  # positive = ran too warm
            ensemble_client.update_bias(city, metric, error_f)
            logger.info(
                "LearningLoop: ensemble bias update for %s %s — "
                "mean=%.1f°F actual=%.1f°F error=%+.1f°F",
                city, metric, float(ens_mean), float(actual_val), error_f,
            )

        # Record full resolution to forecast_errors.json for adaptive sigma
        # and source-skill tracking.  Deduplicated by (city, metric, date).
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
        _save_json(_POSITIONS_FILE, positions)
        _save_json(_SCORES_FILE, scores)
        logger.info("LearningLoop: resolved %d positions.", len(resolved))

    # ── Process skipped signals for ensemble bias only ────────────────────
    # Skips have the full forecast context but no money was bet, so we update
    # ensemble bias (free temperature data). After settlement we also apply a
    # small station-score nudge toward would-have-won outcomes (so the pipeline
    # gains confidence on cities whose model calls would have worked).
    _process_skip_bias()

    return resolved


def _process_skip_bias() -> None:
    """Update ensemble bias from skipped / watched signals whose market day has passed.

    For each unprocessed row in signals.json with action ``skip`` or
    ``signal_watch``, fetch METAR actuals, EMA-update ensemble bias,
    record calibration, and optionally nudge station scores from the
    hypothetical trade outcome.
    """
    signals_path = os.path.join(dp.logs_dir(), "signals.json")
    signals: list[dict] = _load_json(signals_path, [])
    scores: dict[str, float] = _load_json(_SCORES_FILE, {})

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

        # Only try after the market date has passed.
        try:
            mkt_dt = datetime.strptime(market_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if mkt_dt.date() >= datetime.now(timezone.utc).date():
                continue   # market day not yet over
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

            # Record to forecast_errors.json (dedupes by city+metric+date,
            # so position resolutions earlier in this cycle take precedence).
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

            # Would the recommended side have won?
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
                "LearningLoop: skip/watch bias update for %s %s %s — "
                "mean=%.1f°F actual=%.1f°F error=%+.1f°F  would_have_won=%s",
                city, metric, sig.get("ticker", ""),
                float(ens_mean), float(actual_val), error_f,
                sig.get("would_have_won", "—"),
            )

    if signals_dirty:
        _save_json(signals_path, signals)
    if scores_dirty:
        _save_json(_SCORES_FILE, scores)


def generate_summary(hours: int = 24) -> dict:
    """Return a performance summary for positions resolved in the last N hours.

    Args:
        hours: Look-back window in hours (default 24).

    Returns:
        {
            pl:            float – net dollars won/lost
            wins:          int
            losses:        int
            total:         int   – wins + losses
            win_rate:      float – 0–1
            bankroll:      float – live balance (0 if unavailable)
            signals_fired: int   – signals logged today (skips + bets)
            best_city:     str   – city with most wins, or "—"
        }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    positions: list[dict] = _load_json(_POSITIONS_FILE, [])
    signals:   list[dict] = _load_json(_SIGNALS_FILE,   [])

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

    # Best city: most wins in the window.
    city_wins: dict[str, int] = {}
    for p in recent:
        if p.get("status") == "won":
            city = p.get("city", "")
            city_wins[city] = city_wins.get(city, 0) + 1
    best_city = max(city_wins, key=lambda c: city_wins[c]) if city_wins else "—"

    # Signals fired = bets placed + skips logged in the window.
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
    # Also count placed bets (positions opened in the window).
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

    # Live bankroll – attempt to fetch; fall back to 0.
    bankroll = 0.0
    try:
        from pipeline import kalshi_client as kc
        bankroll = kc.get_account_balance()
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


# ---------------------------------------------------------------------------
# MLB Polymarket (``venue`` = ``mlb_poly``) — resolution + daily summary
# ---------------------------------------------------------------------------

VENUE_MLB_POLY = "mlb_poly"


def _mlb_team_key(abbr: str) -> str:
    a = (abbr or "").strip().upper()
    return f"mlb_{a}" if a else ""


def _game_final_runs(mlb_api, game_pk: int) -> tuple[bool, int | None, int | None]:
    """Return ``(is_final_decisive, home_runs, away_runs)``; ties are not decisive."""
    try:
        feed = mlb_api.get_game_feed_live(int(game_pk))
    except Exception as exc:
        logger.warning("MLB learning: feed/live failed for %s — %s", game_pk, exc)
        return False, None, None
    gd = feed.get("gameData") or {}
    status = gd.get("status") or {}
    abstract = str(status.get("abstractGameState") or "").lower()
    detailed = str(status.get("detailedState") or "").lower()
    is_final = abstract == "final" or detailed == "final" or "final" in detailed
    if not is_final:
        return False, None, None
    ls = (feed.get("liveData") or {}).get("linescore") or {}
    teams = ls.get("teams") or {}
    try:
        hr = int((teams.get("home") or {}).get("runs"))
        ar = int((teams.get("away") or {}).get("runs"))
    except (TypeError, ValueError):
        return True, None, None
    if hr == ar:
        return False, hr, ar
    return True, hr, ar


def _yes_paid_out(home_won: bool, yes_is_home: bool) -> bool:
    return (home_won and yes_is_home) or ((not home_won) and (not yes_is_home))


def _mlb_bet_won(recommended_side: str, home_won: bool, yes_is_home: bool) -> bool:
    y = _yes_paid_out(home_won, yes_is_home)
    rec = (recommended_side or "yes").lower()
    if rec == "yes":
        return y
    if rec == "no":
        return not y
    return False


def _predicted_home_win_from_pos(pos: dict) -> bool:
    if pos.get("predicted_home_win") is not None:
        return bool(pos.get("predicted_home_win"))
    return float(pos.get("model_home_prob", 0.5)) > 0.5


def _nudge_mlb_team_model(scores: dict[str, float], abbr: str, model_correct: bool) -> None:
    k = _mlb_team_key(abbr)
    if not k or k == "mlb_":
        return
    current = float(scores.get(k, 1.0))
    mult = _SCORE_WIN_MULT if model_correct else _SCORE_LOSS_MULT
    scores[k] = round(max(_SCORE_MIN, min(_SCORE_MAX, current * mult)), 4)


def _nudge_mlb_team_watch(scores: dict[str, float], abbr: str, would_won: bool) -> None:
    k = _mlb_team_key(abbr)
    if not k or k == "mlb_":
        return
    current = float(scores.get(k, 1.0))
    mult = _WATCH_WIN_MULT if would_won else _WATCH_LOSS_MULT
    scores[k] = round(max(_SCORE_MIN, min(_SCORE_MAX, current * mult)), 4)


def _apply_mlb_outcome_learning(
    *,
    home_pitcher_id: int | None,
    away_pitcher_id: int | None,
    home_team_abbr: str,
    away_team_abbr: str,
    home_won: bool,
    predicted_home_win: bool,
    scores: dict[str, float],
) -> None:
    import pipeline.pitcher_analyzer as pa

    if home_pitcher_id:
        pa.update_pitcher_score(int(home_pitcher_id), (home_won == predicted_home_win))
    if away_pitcher_id:
        pa.update_pitcher_score(
            int(away_pitcher_id),
            ((not home_won) == (not predicted_home_win)),
        )
    _nudge_mlb_team_model(scores, home_team_abbr, (home_won == predicted_home_win))
    _nudge_mlb_team_model(
        scores, away_team_abbr, ((not home_won) == (not predicted_home_win))
    )


def _mlb_position_already_learned(game_id: int) -> bool:
    """True if we already applied model-based pitcher/team updates for this game."""
    for p in _load_json(_POSITIONS_FILE, []):
        if int(p.get("game_id") or 0) != int(game_id):
            continue
        if p.get("venue") != VENUE_MLB_POLY:
            continue
        if p.get("status") in ("won", "lost"):
            return True
    return False


def _mlb_games_with_umpire_updated() -> set[int]:
    """Games whose final score was already fed into ``umpire_tracker`` (resolved bets)."""
    out: set[int] = set()
    for p in _load_json(_POSITIONS_FILE, []):
        if p.get("venue") != VENUE_MLB_POLY:
            continue
        if p.get("status") not in ("won", "lost"):
            continue
        gid = p.get("game_id")
        if gid is not None:
            out.add(int(gid))
    return out


def check_mlb_resolutions(mlb_client, polymarket_client=None) -> list[dict]:
    """Resolve open ``mlb_poly`` positions from MLB final scores; learn skips.

    Args:
        mlb_client: ``pipeline.mlb_client`` module (pass the module, not a class).
        polymarket_client: Reserved for future settlement cross-checks; unused.

    Returns:
        Position dicts newly marked won/lost.
    """
    _ = polymarket_client
    positions: list[dict] = _load_json(_POSITIONS_FILE, [])
    scores: dict[str, float] = _load_json(_SCORES_FILE, {})
    resolved: list[dict] = []
    umpire_games: set[int] = _mlb_games_with_umpire_updated()

    for pos in positions:
        if pos.get("venue") != VENUE_MLB_POLY:
            continue
        if pos.get("status") != "open":
            continue
        gid = pos.get("game_id")
        if gid is None:
            continue
        fin, hr, ar = _game_final_runs(mlb_client, int(gid))
        if not fin or hr is None or ar is None:
            continue
        home_won = hr > ar
        predicted_home = _predicted_home_win_from_pos(pos)

        import pipeline.umpire_tracker as ut

        if int(gid) not in umpire_games:
            ut.update_after_game(int(gid), hr + ar)
            umpire_games.add(int(gid))

        _apply_mlb_outcome_learning(
            home_pitcher_id=pos.get("home_pitcher_id"),
            away_pitcher_id=pos.get("away_pitcher_id"),
            home_team_abbr=str(pos.get("home_team_abbr") or ""),
            away_team_abbr=str(pos.get("away_team_abbr") or ""),
            home_won=home_won,
            predicted_home_win=predicted_home,
            scores=scores,
        )

        yes_is_home = bool(pos.get("yes_is_home"))
        won = _mlb_bet_won(str(pos.get("recommended_side")), home_won, yes_is_home)
        price_cents = int(pos.get("price_cents") or 50)
        contracts = int(pos.get("kelly_contracts") or 1)
        if won:
            pl = (1 - price_cents / 100) * contracts
            pos["status"] = "won"
        else:
            pl = -(price_cents / 100) * contracts
            pos["status"] = "lost"
        pos["pl"] = round(pl, 4)
        pos["resolved_at"] = datetime.now(timezone.utc).isoformat()
        pos["actual_result"] = "home_win" if home_won else "away_win"
        pos["yes_paid_out"] = _yes_paid_out(home_won, yes_is_home)
        logger.info(
            "MLB learning: position game %s → %s P&L=$%.2f",
            gid,
            pos["status"],
            pl,
        )
        resolved.append(pos)

    if resolved:
        _save_json(_POSITIONS_FILE, positions)
        _save_json(_SCORES_FILE, scores)

    _process_mlb_skips(mlb_client, scores)
    return resolved


def _bet_target_abbr(sig: dict, home_abbr: str, away_abbr: str) -> str:
    rec = str(sig.get("recommended_side", "yes")).lower()
    yh = bool(sig.get("yes_is_home"))
    if rec == "yes":
        return home_abbr if yh else away_abbr
    # NO wins when YES loses
    return away_abbr if yh else home_abbr


def _process_mlb_skips(mlb_client, scores: dict[str, float]) -> None:
    """Finalize MLB skip / watch rows after games end (pitcher + soft team nudge)."""
    signals: list[dict] = _load_json(_SIGNALS_FILE, [])
    dirty = False
    model_done: set[int] = set()
    ump_done: set[int] = _mlb_games_with_umpire_updated()

    for sig in signals:
        if sig.get("venue") != VENUE_MLB_POLY:
            continue
        if sig.get("action") not in signal_learning_log.LEARNING_ACTIONS:
            continue
        if sig.get("mlb_resolved"):
            continue
        gid = sig.get("game_id")
        if gid is None:
            continue
        fin, hr, ar = _game_final_runs(mlb_client, int(gid))
        if not fin or hr is None or ar is None:
            continue
        home_won = hr > ar
        home_abbr = str(sig.get("home_team_abbr") or "")
        away_abbr = str(sig.get("away_team_abbr") or "")
        yes_is_home = bool(sig.get("yes_is_home"))
        predicted_home = bool(sig.get("predicted_home_win"))
        if sig.get("predicted_home_win") is None:
            predicted_home = float(sig.get("model_home_prob", 0.5)) > 0.5

        import pipeline.umpire_tracker as ut

        if int(gid) not in ump_done:
            ut.update_after_game(int(gid), hr + ar)
            ump_done.add(int(gid))

        if int(gid) not in model_done and not _mlb_position_already_learned(int(gid)):
            _apply_mlb_outcome_learning(
                home_pitcher_id=sig.get("home_pitcher_id"),
                away_pitcher_id=sig.get("away_pitcher_id"),
                home_team_abbr=home_abbr,
                away_team_abbr=away_abbr,
                home_won=home_won,
                predicted_home_win=predicted_home,
                scores=scores,
            )
        model_done.add(int(gid))

        would_won = _mlb_bet_won(str(sig.get("recommended_side")), home_won, yes_is_home)
        sig["would_have_won"] = would_won
        sig["mlb_resolved"] = True
        sig["home_runs_final"] = hr
        sig["away_runs_final"] = ar
        dirty = True

        tgt = _bet_target_abbr(sig, home_abbr, away_abbr)
        if tgt and not sig.get("mlb_watch_nudged"):
            _nudge_mlb_team_watch(scores, tgt, would_won)
            sig["mlb_watch_nudged"] = True

    if dirty:
        _save_json(_SIGNALS_FILE, signals)
        _save_json(_SCORES_FILE, scores)


def generate_mlb_daily_summary(hours: int = 24) -> dict:
    """Summary for ``mlb_poly`` positions resolved in the last *hours*."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    positions: list[dict] = _load_json(_POSITIONS_FILE, [])
    signals: list[dict] = _load_json(_SIGNALS_FILE, [])

    recent: list[dict] = []
    for pos in positions:
        if pos.get("venue") != VENUE_MLB_POLY:
            continue
        rs = pos.get("resolved_at")
        if not rs:
            continue
        try:
            rt = datetime.fromisoformat(rs)
            if rt.tzinfo is None:
                rt = rt.replace(tzinfo=timezone.utc)
            if rt >= cutoff:
                recent.append(pos)
        except (ValueError, TypeError):
            continue

    pl_total = sum(p.get("pl", 0) or 0 for p in recent)
    wins = sum(1 for p in recent if p.get("status") == "won")
    losses = sum(1 for p in recent if p.get("status") == "lost")
    total = wins + losses
    win_rate = wins / total if total else 0.0

    best_pitcher = "—"
    worst_pitcher = "—"
    if recent:
        win_pick = max(
            (p for p in recent if p.get("status") == "won"),
            key=lambda x: float(x.get("pl") or 0),
            default=None,
        )
        loss_pick = min(
            (p for p in recent if p.get("status") == "lost"),
            key=lambda x: float(x.get("pl") or 0),
            default=None,
        )
        if win_pick:
            best_pitcher = str(win_pick.get("focus_pitcher_name") or "—")
        if loss_pick:
            worst_pitcher = str(loss_pick.get("focus_pitcher_name") or "—")

    signals_fired = 0
    for sig in signals:
        if sig.get("venue") != VENUE_MLB_POLY:
            continue
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
        if pos.get("venue") != VENUE_MLB_POLY:
            continue
        placed_str = pos.get("placed_at", "")
        try:
            placed = datetime.fromisoformat(placed_str)
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=timezone.utc)
            if placed >= cutoff:
                signals_fired += 1
        except (ValueError, TypeError):
            continue

    top_edge = 0.0
    top_lbl = "—"
    for sig in signals:
        if sig.get("venue") != VENUE_MLB_POLY:
            continue
        ts_str = sig.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        edge = float(sig.get("edge") or 0)
        pf = float(sig.get("park_run_factor") or 1.0)
        combo = abs(edge) * abs(pf - 1.0)
        if combo >= top_edge:
            top_edge = combo
            v = sig.get("venue_name") or sig.get("city") or "—"
            top_lbl = f"{v} · rf={pf:.2f} · edge={edge * 100:+.1f}%"

    bankroll = 0.0
    try:
        bankroll = float(poly_client.get_account_balance())
    except Exception:
        try:
            from pipeline import kalshi_client as kc

            bankroll = float(kc.get_account_balance())
        except Exception:
            pass

    return {
        "pl": round(pl_total, 2),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "bankroll": round(bankroll, 2),
        "signals_fired": signals_fired,
        "best_pitcher": best_pitcher,
        "worst_pitcher": worst_pitcher,
        "top_park_factor_edge": top_lbl,
    }


def generate_daily_summary() -> dict:
    """Alias for :func:`generate_mlb_daily_summary` (24 h window)."""
    return generate_mlb_daily_summary(24)
