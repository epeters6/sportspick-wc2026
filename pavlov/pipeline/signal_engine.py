"""
pipeline/signal_engine.py – Edge calculation engine for pavlov-weather-bot.

Compares NWS temperature forecasts against Kalshi implied probabilities to
find markets with a positive expected edge.

Public API
----------
parse_market(market)              -> dict | None
calculate_edge(market, bankroll)  -> dict | None
get_all_signals(markets, bankroll) -> list[dict]
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal

from config import CONFIG
import data_paths as dp
from pipeline.nws_client import get_predicted_high, get_predicted_low
from pipeline.station_mapper import STATION_MAP, get_city_for_market
import pipeline.owm_client as owm_client
import pipeline.ensemble_client as ensemble_client
import pipeline.metar_client as metar_client
import pipeline.calibration_log as calibration_log
import pipeline.kalshi_client as kalshi_client

logger = logging.getLogger(__name__)

_SCORES_FILE    = os.path.join(dp.logs_dir(), "station_scores.json")
_POSITIONS_FILE = os.path.join(dp.logs_dir(), "positions.json")

_sig_tls = threading.local()


def _active_scores_file() -> str:
    path = getattr(_sig_tls, "scores_file", None)
    return path if path else _SCORES_FILE


def _active_positions_file() -> str:
    path = getattr(_sig_tls, "positions_file", None)
    return path if path else _POSITIONS_FILE


@contextmanager
def use_station_score_paths(scores_file: str, positions_file: str):
    """Use alternate station_scores / positions paths (e.g. Polymarket)."""
    prev_s = getattr(_sig_tls, "scores_file", None)
    prev_p = getattr(_sig_tls, "positions_file", None)
    _sig_tls.scores_file = scores_file
    _sig_tls.positions_file = positions_file
    try:
        yield
    finally:
        if prev_s is None:
            if hasattr(_sig_tls, "scores_file"):
                delattr(_sig_tls, "scores_file")
        else:
            _sig_tls.scores_file = prev_s
        if prev_p is None:
            if hasattr(_sig_tls, "positions_file"):
                delattr(_sig_tls, "positions_file")
        else:
            _sig_tls.positions_file = prev_p

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _load_station_scores() -> dict[str, float]:
    """Load station scores from logs/station_scores.json.

    Values are expected to be floats near 1.0.  Any city not present gets a
    default score of 1.0 (neutral weight).
    """
    raw = _load_json(_active_scores_file(), {})
    scores: dict[str, float] = {}
    for city, value in raw.items():
        try:
            scores[city] = float(value)
        except (TypeError, ValueError):
            scores[city] = 1.0
    return scores


def _load_traded_tickers() -> set[str]:
    """Tickers with an open position in the bot log *or* non-zero Kalshi exchange size.

    Merging the portfolio API avoids duplicate signals / auto-bets after a
    redeploy wipes ``logs/positions.json``.
    """
    positions = _load_json(_active_positions_file(), [])
    out: set[str] = set()
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("venue") == "poly_us":
            continue
        if p.get("status") == "open":
            t = p.get("ticker")
            if t:
                out.add(t)
    pos_path = _active_positions_file().replace("\\", "/")
    is_poly_track = "/logs_poly/" in pos_path or pos_path.endswith("/logs_poly/positions.json")

    try:
        if not is_poly_track:
            for row in kalshi_client.get_open_positions():
                t = row.get("ticker")
                if t:
                    out.add(t)
    except Exception as exc:
        logger.warning("SignalEngine: could not merge Kalshi exchange positions — %s", exc)
    return out


# ---------------------------------------------------------------------------
# Market title parsing
# ---------------------------------------------------------------------------

# Patterns tried in order.  Each captures optional city context plus the
# core predicate (metric / direction / threshold).
_PARSE_PATTERNS: list[re.Pattern] = [
    # PRIMARY – actual Kalshi format:
    # "Will the high temp in NYC be >90° on May 17, 2026?"
    # Also handles <X° for low-temp markets.
    re.compile(
        r"\b(?P<metric_word>high|low)\b.{0,60}?"
        r"(?P<op>[><])(?P<threshold>\d{2,3})\s*[°oF]*",
        re.IGNORECASE,
    ),
    # Bare ">90°" / "<32°F" anywhere in title (fallback for edge cases)
    re.compile(
        r"(?P<op>[><])(?P<threshold>\d{2,3})\s*[°oF]+",
        re.IGNORECASE,
    ),
    # "Will Chicago reach 85F today?" / "Will Miami hit 95 degrees?"
    re.compile(
        r"(?P<city_hint>.+?)?\b(?:reach|hit|exceed|top)\b.{0,30}?"
        r"(?P<threshold>\d{2,3})\s*(?:f|°f|degrees?)",
        re.IGNORECASE,
    ),
    # "Will Miami stay below 60F?" / "Will it remain under 50°F?"
    re.compile(
        r"(?P<city_hint>.+?)?\b(?:stay|remain|be|drop)\b.{0,30}?"
        r"(?P<direction_word>below|under)\s*(?P<threshold>\d{2,3})\s*(?:f|°f|degrees?)",
        re.IGNORECASE,
    ),
    # "High above 90F in Dallas" / "Low below 32F"
    re.compile(
        r"\b(?P<metric_word>high|low)\b.{0,40}?"
        r"(?P<direction_word>above|below|over|under)\s*(?P<threshold>\d{2,3})\s*(?:f|°f|degrees?)?",
        re.IGNORECASE,
    ),
    # "Dallas high temperature above 100" / "Chicago low below 20"
    re.compile(
        r"(?P<city_hint>.+?)?"
        r"\b(?P<metric_word>high|low)\b.{0,30}?"
        r"(?P<direction_word>above|below|over|under)\s*(?P<threshold>\d{2,3})",
        re.IGNORECASE,
    ),
    # Bare threshold with direction word anywhere: "above 95 in NYC"
    re.compile(
        r"\b(?P<direction_word>above|below|over|under)\s+(?P<threshold>\d{2,3})\b",
        re.IGNORECASE,
    ),
]

# Separate pattern for Kalshi range markets: "89-90°" or "85-86°F"
_RANGE_PATTERN = re.compile(
    r"\b(?P<lo>\d{2,3})-(?P<hi>\d{2,3})\s*[°oF]*",
    re.IGNORECASE,
)

_ABOVE_WORDS = {"above", "over", "reach", "hit", "exceed", "top"}
_BELOW_WORDS = {"below", "under", "stay", "remain", "drop"}


def _extract_direction(match: re.Match, title: str) -> Literal["above", "below"] | None:
    """Determine direction from a regex match or by scanning the full title.

    Handles both word-based directions (above/below) and operator-based
    directions (>/< as used in actual Kalshi market titles).
    """
    # Check for > / < operator captured by the new primary patterns.
    try:
        op = match.group("op")
        if op == ">":
            return "above"
        if op == "<":
            return "below"
    except IndexError:
        pass

    try:
        dw = match.group("direction_word").lower()
    except IndexError:
        dw = ""

    if dw in _ABOVE_WORDS:
        return "above"
    if dw in _BELOW_WORDS:
        return "below"

    # Fall back to scanning the whole title for > / < operators.
    if ">" in title:
        return "above"
    if "<" in title:
        return "below"

    # Final fallback: word scan.
    norm = title.lower()
    for word in _ABOVE_WORDS:
        if re.search(r"\b" + word + r"\b", norm):
            return "above"
    for word in _BELOW_WORDS:
        if re.search(r"\b" + word + r"\b", norm):
            return "below"
    return None



def _extract_metric(match: re.Match, title: str) -> Literal["high", "low"]:
    """Return 'high' or 'low' based on the match group or title keywords."""
    try:
        mw = match.group("metric_word").lower()
        if mw in ("high", "low"):
            return mw  # type: ignore[return-value]
    except IndexError:
        pass
    return _extract_metric_from_title(title)


def _extract_metric_from_title(title: str) -> Literal["high", "low"]:
    """Return 'high' or 'low' by scanning the title (no match object needed)."""
    norm = title.lower()
    if re.search(r"\blow\b", norm):
        return "low"
    return "high"  # default


def measurement_date_str(market: dict) -> str:
    """Infer the market's measurement calendar day (YYYY-MM-DD) from ``close_time``.

    Same rule as ``calculate_edge``: early-morning UTC closes imply the prior
    calendar day is the NWS measurement day.
    """
    from datetime import timedelta

    close_time_raw = market.get("close_time", "") or ""
    if not close_time_raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        close_dt = datetime.fromisoformat(close_time_raw.replace("Z", "+00:00"))
        if close_dt.hour < 12:
            market_d = (close_dt - timedelta(days=1)).date()
        else:
            market_d = close_dt.date()
        return market_d.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_market(market: dict) -> dict | None:
    """Parse a Kalshi market dict into a structured signal descriptor.

    Handles two Kalshi title formats:
      1. "Will the high/low temp in {CITY} be >X° on {date}?"
         — city and metric extracted from title text.
      2. "Will the maximum/minimum temperature be >X° on {date}?"
         — KXHIGHT*/KXLOWT* series; city and metric taken from the
           ``city_hint`` / ``metric_hint`` fields set by kalshi_client.

    Direction is determined from ``strike_type`` (set by kalshi_client's
    _parse_market), which is authoritative.  Falls back to scanning the
    title for > / < symbols or a range pattern when strike_type is absent
    (e.g. simulate mode fake markets).

    Returns None if metric or city cannot be determined.
    """
    title = market.get("title", "")
    title = title.replace("**", "").replace("*", "")
    title_lower = title.lower()

    # ── Determine metric ──────────────────────────────────────────────────
    if (
        "high temp" in title_lower
        or "high temperature" in title_lower
        or "highest temp" in title_lower
        or "highest temperature" in title_lower
    ):
        metric = "high"
    elif (
        "low temp" in title_lower
        or "low temperature" in title_lower
        or "lowest temp" in title_lower
        or "lowest temperature" in title_lower
    ):
        metric = "low"
    elif "maximum temperature" in title_lower or "maximum temp" in title_lower:
        metric = "high"
    elif "minimum temperature" in title_lower or "minimum temp" in title_lower:
        metric = "low"
    else:
        metric_hint = market.get("metric_hint", "")
        if metric_hint in ("high", "low"):
            metric = metric_hint
        else:
            return None

    # ── Determine city ────────────────────────────────────────────────────
    # Title-based aliases (covers KXHIGH{CITY} series and simulate mode).
    _CITY_ALIASES: dict[str, str] = {
        "NYC":           "New York",
        "New York":      "New York",
        "Chicago":       "Chicago",
        "Miami":         "Miami",
        "Dallas":        "Dallas",
        "Denver":        "Denver",
        "Seattle":       "Seattle",
        "Boston":        "Boston",
        "Atlanta":       "Atlanta",
        "Phoenix":       "Phoenix",
        "Minneapolis":   "Minneapolis",
        "Las Vegas":     "Las Vegas",
        "Los Angeles":   "Los Angeles",
        "LA":            "Los Angeles",
        "San Francisco": "San Francisco",
        "Washington DC": "Washington DC",
        "DC":            "Washington DC",
        "Philadelphia":  "Philadelphia",
        "Austin":        "Austin",
        "Houston":       "Houston",
        "San Antonio":   "San Antonio",
        "Oklahoma City": "Oklahoma City",
        "OKC":           "Oklahoma City",
    }

    city = None
    for alias, canonical in _CITY_ALIASES.items():
        if alias in title:
            city = canonical
            break

    # Fallback: use city_hint embedded by kalshi_client from the series ticker.
    # This is the only source for KXHIGHT*/KXLOWT* markets whose titles
    # say "Will the maximum temperature be >X°" with no city name.
    if not city:
        city = market.get("city_hint", "") or None
    if not city:
        return None

    # --- Direction: use strike_type if available (most reliable) -----------
    strike_type = market.get("strike_type", "")

    if strike_type == "greater":
        threshold = market.get("floor_strike")
        if threshold is None:
            m = re.search(r'>(\d+)', title)
            threshold = float(m.group(1)) if m else None
        if threshold is None:
            return None
        return {
            "city":         city,
            "metric":       metric,
            "threshold_f":  float(threshold),
            "threshold_lo": None,
            "threshold_hi": None,
            "direction":    "above",
        }

    if strike_type == "less":
        cap = market.get("ceiling_strike")
        if cap is not None:
            c = float(cap)
            return {
                "city":         city,
                "metric":       metric,
                "threshold_f":  c,
                "threshold_lo": None,
                "threshold_hi": None,
                "direction":    "below",
            }
        m = re.search(r'<(\d+)', title)
        if not m:
            return None
        return {
            "city":         city,
            "metric":       metric,
            "threshold_f":  float(m.group(1)),
            "threshold_lo": None,
            "threshold_hi": None,
            "direction":    "below",
        }

    if strike_type == "between":
        lo_raw = market.get("threshold_lo")
        hi_raw = market.get("threshold_hi")
        if lo_raw is not None and hi_raw is not None:
            lo, hi = float(lo_raw), float(hi_raw)
            return {
                "city":         city,
                "metric":       metric,
                "threshold_f":  (lo + hi) / 2,
                "threshold_lo": lo,
                "threshold_hi": hi,
                "direction":    "in_range",
            }
        m = re.search(r'(\d+)\s*(?:-|–|—|to)\s*(\d+)', title_lower)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            return {
                "city":         city,
                "metric":       metric,
                "threshold_f":  (lo + hi) / 2,
                "threshold_lo": lo,
                "threshold_hi": hi,
                "direction":    "in_range",
            }
        m = re.search(r'(\d+)-(\d+)', title)
        if not m:
            return None
        lo, hi = float(m.group(1)), float(m.group(2))
        return {
            "city":         city,
            "metric":       metric,
            "threshold_f":  (lo + hi) / 2,
            "threshold_lo": lo,
            "threshold_hi": hi,
            "direction":    "in_range",
        }

    # --- Fallback: parse from title symbols (simulate mode, etc.) ----------
    m = re.search(r'>(\d+)', title)
    if m:
        return {
            "city":         city,
            "metric":       metric,
            "threshold_f":  float(m.group(1)),
            "threshold_lo": None,
            "threshold_hi": None,
            "direction":    "above",
        }

    m = re.search(r'<(\d+)', title)
    if m:
        return {
            "city":         city,
            "metric":       metric,
            "threshold_f":  float(m.group(1)),
            "threshold_lo": None,
            "threshold_hi": None,
            "direction":    "below",
        }

    m = re.search(r'(\d+)-(\d+)', title)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return {
            "city":         city,
            "metric":       metric,
            "threshold_f":  (lo + hi) / 2,
            "threshold_lo": lo,
            "threshold_hi": hi,
            "direction":    "in_range",
        }

    return None


# ---------------------------------------------------------------------------
# Edge calculation
# ---------------------------------------------------------------------------

def calculate_edge(market: dict, bankroll: float, trading_mode: bool = True, num_comparisons: int = 1) -> dict | None:
    suppressed_reason = None
    """Calculate the trading edge for a single Kalshi market.

    Args:
        market:    A parsed market dict from kalshi_client.get_weather_markets().
        bankroll:  Current account balance in dollars.

    Returns:
        A full signal dict (see module docstring) or None if the market should
        be skipped (unparseable, NWS data unavailable, etc.).
    """
    # 1. Parse the market title.
    parsed = parse_market(market)
    if parsed is None:
        return None

    city        = parsed["city"]
    metric      = parsed["metric"]
    threshold_f = parsed["threshold_f"]
    direction   = parsed["direction"]
    threshold_lo = parsed.get("threshold_lo")
    threshold_hi = parsed.get("threshold_hi")

    date_str = measurement_date_str(market)
    try:
        if metric == "high":
            nws_data   = get_predicted_high(city, date_str)
            nws_value  = nws_data["predicted_high_f"]
        else:
            nws_data   = get_predicted_low(city, date_str)
            nws_value  = nws_data["predicted_low_f"]
    except Exception as exc:
        logger.warning(
            "SignalEngine: NWS fetch failed for %s %s - %s", city, metric, exc
        )
        return None

    # 3. Implied probability — use bid-ask midpoint when both sides are
    #    available.  The midpoint is a better estimate of the true market
    #    price than the ask alone, which systematically overstates the cost
    #    and understates edge.  Falls back to ask-only if bid is missing.
    yes_ask = market.get("yes_ask")
    yes_bid = market.get("yes_bid")
    if yes_ask is None:
        logger.debug("SignalEngine: no yes_ask for %s", market.get("ticker"))
        return None
    if yes_bid is not None and yes_bid > 0:
        implied_prob: float = (yes_bid + yes_ask) / 200.0
    else:
        implied_prob = yes_ask / 100.0

    # ── Time/horizon/station context (computed once, used throughout) ─────
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        target_dt = datetime.strptime(date_str, "%Y-%m-%d")
        today_dt  = datetime.strptime(today_str, "%Y-%m-%d")
        days_out  = (target_dt - today_dt).days
    except (ValueError, TypeError):
        days_out = 0

    hours_left = 999.0
    close_time_str = market.get("close_time", "")
    if close_time_str:
        try:
            close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            hours_left = max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass

    station_id = STATION_MAP.get(city, {}).get("station", "")

    # 4a. OWM second-source consensus check.
    #
    # Disagreement buckets (delta = |NWS − OWM|):
    #   <=1.5°F  → agree:   both models confident, reduce Gaussian sigma by 20%
    #   1.5-3°F  → close:   minor disagreement, no sigma change
    #   3-5°F    → diverge: notable disagreement — widen sigma 35% and require
    #                        larger edge (spread_penalty * 1.20)
    #   >5°F     → hard suppress (fundamental model disagreement)
    #
    # Previously the hard-suppress threshold was 3°F, which was too aggressive
    # and discarded many valid signals where OWM lags NWS update cycles.
    owm_value: float | None = None
    source_agreement = "nws_only"
    owm_disagreement_penalty = 1.0   # fed into spread_penalty below
    if owm_client.available():
        try:
            if metric == "high":
                owm_value = owm_client.get_predicted_high(city, date_str)
            else:
                owm_value = owm_client.get_predicted_low(city, date_str)
        except Exception as exc:
            logger.debug("SignalEngine: OWM fetch failed for %s – %s", city, exc)

        if owm_value is not None:
            delta = abs(owm_value - nws_value)
            if delta > 5.0:
                logger.debug(
                    "SignalEngine: NWS/OWM hard disagreement %.1f°F for %s – suppressing",
                    delta, market.get("ticker"),
                )
                if trading_mode: return None
                else: suppressed_reason = "owm_disagreement"
            elif delta > 3.0:
                source_agreement        = "diverge"
                owm_disagreement_penalty = 1.20   # require 20% larger edge
                logger.debug(
                    "SignalEngine: NWS/OWM diverge %.1f°F for %s – applying penalty",
                    delta, market.get("ticker"),
                )
            elif delta <= 1.5:
                source_agreement = "agree"
            else:
                source_agreement = "close"

    # 4b. Model probability — ensemble-first architecture.
    #
    #  Tier 1 (best): GFS + ECMWF ensemble vote-count (~82 members, free via
    #                 Open-Meteo). Direct probability — no approximation needed.
    #  Tier 2 (good): NWS + optional OWM Gaussian model with per-city sigma.
    #  The ensemble is preferred because it captures nonlinear forecast uncertainty
    #  that a fixed-sigma Gaussian cannot.

    ensemble_result = ensemble_client.get_ensemble_prob(
        city=city,
        date_str=date_str,
        threshold_f=threshold_f,
        direction=direction,
        metric=metric,
        threshold_lo=threshold_lo,
        threshold_hi=threshold_hi,
    )

    ensemble_members = 0
    ensemble_mean    = None
    ensemble_spread  = None

    if ensemble_result and ensemble_result["members"] >= 10:
        # Ensemble vote-count available — use directly.
        confidence = ensemble_result["prob"]
        ensemble_members = ensemble_result["members"]
        ensemble_mean    = ensemble_result["mean_f"]
        ensemble_spread  = ensemble_result["spread_f"]
        # NWS margin still useful for display.
        if direction == "in_range" and threshold_lo is not None and threshold_hi is not None:
            in_range = threshold_lo <= nws_value <= threshold_hi
            margin = 0.0 if in_range else min(
                abs(nws_value - threshold_lo), abs(nws_value - threshold_hi)
            )
        else:
            margin = abs(nws_value - threshold_f)
        source_agreement = f"ensemble:{ensemble_members}m"
        logger.debug(
            "SignalEngine: ensemble %d members → %.1f%% for %s",
            ensemble_members, confidence * 100, market.get("ticker")
        )
    else:
        # Fallback: Gaussian model on NWS (+ OWM agreement check).
        # Overnight lows are harder to forecast than daytime highs — they
        # depend on cloud cover, humidity, and wind, which NWS gridded
        # forecasts model with less skill than solar-driven temperature rise.
        # Low-sigma values are therefore wider by ~0.5°F across all cities.
        _NWS_SIGMA_HIGH: dict[str, float] = {
            "New York": 2.2, "Chicago": 2.8, "Miami": 1.8, "Denver": 3.2,
            "Dallas": 2.5, "Phoenix": 2.0, "Seattle": 2.5, "Boston": 2.3,
            "Atlanta": 2.4, "Las Vegas": 2.1, "Minneapolis": 3.0,
            "Los Angeles": 1.9, "San Francisco": 2.2, "Washington DC": 2.3,
            "Philadelphia": 2.3, "Austin": 2.6, "Houston": 2.4,
            "San Antonio": 2.5, "Oklahoma City": 2.7,
        }
        _NWS_SIGMA_LOW: dict[str, float] = {
            "New York": 2.7, "Chicago": 3.3, "Miami": 2.3, "Denver": 3.7,
            "Dallas": 3.0, "Phoenix": 2.5, "Seattle": 3.0, "Boston": 2.8,
            "Atlanta": 2.9, "Las Vegas": 2.6, "Minneapolis": 3.5,
            "Los Angeles": 2.4, "San Francisco": 2.7, "Washington DC": 2.8,
            "Philadelphia": 2.8, "Austin": 3.1, "Houston": 2.9,
            "San Antonio": 3.0, "Oklahoma City": 3.2,
        }
        _sigma_table = _NWS_SIGMA_LOW if metric == "low" else _NWS_SIGMA_HIGH
        default_sigma = _sigma_table.get(city, 3.0 if metric == "low" else 2.5)
        # Adaptive sigma: if we have ≥8 historical resolutions for this
        # (city, metric), blend in the measured stdev of ensemble errors.
        # Below 8 records this returns default_sigma unchanged.
        sigma = calibration_log.get_adaptive_sigma(city, metric, default_sigma)
        if source_agreement == "agree":
            sigma *= 0.80
        elif source_agreement == "diverge":
            # OWM and NWS diverge 3-5°F — widen the Gaussian to reflect that
            # neither model is clearly right.
            sigma *= 1.35

        # Trajectory adjustment for same-day markets.
        #
        # Once we have a current METAR observation we can make two improvements:
        #
        #  (A) Temperature FLOOR (high markets only):
        #      The daily high cannot be below what we've already observed.
        #      If NWS's cached forecast is slightly stale and current temp
        #      already exceeds it, floor nws_value at metar_temp so the
        #      Gaussian is centred correctly.
        #
        #  (B) Sigma SHRINK (both metrics):
        #      With fewer hours remaining there is less meteorological
        #      uncertainty.  Scale sigma linearly from full at 18 h out down
        #      to 35 % of full at market close.  This prevents the Gaussian
        #      from assigning unrealistically wide probability tails when
        #      only 2-3 hours of temperature trajectory remain.
        if days_out == 0 and station_id and hours_left < 18.0:
            current_obs = metar_client.get_current_temp(station_id)
            if current_obs is not None:
                if metric == "high":
                    nws_value = max(nws_value, current_obs)
                    logger.debug(
                        "SignalEngine: trajectory floor %s → %.1f°F (obs=%.1f°F).",
                        market.get("ticker"), nws_value, current_obs,
                    )
                # Sigma shrink: linearly 1.0 → 0.35 over [18h → 0h].
                time_factor = max(0.35, hours_left / 18.0)
                sigma *= time_factor

        def _normal_cdf(x: float, mu: float, s: float) -> float:
            return 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))

        if direction == "in_range" and threshold_lo is not None and threshold_hi is not None:
            p_in = _normal_cdf(threshold_hi, nws_value, sigma) - _normal_cdf(threshold_lo, nws_value, sigma)
            confidence = max(0.001, min(0.999, p_in))
            in_range = threshold_lo <= nws_value <= threshold_hi
            margin = 0.0 if in_range else min(
                abs(nws_value - threshold_lo), abs(nws_value - threshold_hi)
            )
        else:
            if direction == "above":
                p_yes = 1.0 - _normal_cdf(threshold_f, nws_value, sigma)
            else:
                p_yes = _normal_cdf(threshold_f, nws_value, sigma)
            confidence = max(0.001, min(0.999, p_yes))
            margin = abs(nws_value - threshold_f)

    station_scores = _load_station_scores()
    station_score  = station_scores.get(city, 1.0)
    # Keep probability calibrated — do NOT multiply confidence by the score.
    # The score is used later to scale Kelly sizing (bet more on trusted cities,
    # less on cities with poor recent performance).
    model_prob_raw: float = max(0.001, min(0.999, confidence))

    # 4c-i. Ensemble spread filter.
    #   If ensemble_spread > 6°F, models fundamentally disagree (bimodal
    #   distributions like GFS=85°F, ICON=73°F). Suppress the signal entirely.
    #   If spread 4-6°F, require proportionally larger edge later.
    spread_penalty = 1.0   # multiplied against min_edge threshold
    if ensemble_spread is not None:
        if ensemble_spread > 6.0:
            logger.debug(
                "SignalEngine: suppressing %s — ensemble spread %.1f°F too large",
                market.get("ticker"), ensemble_spread,
            )
            if trading_mode: return None
            else: suppressed_reason = "ensemble_spread_too_large"
        elif ensemble_spread > 4.0:
            # Require 25% larger edge for uncertain forecasts.
            spread_penalty = 1.25

    # 4c-ii. Forecast horizon scaling.
    #   Ensemble accuracy degrades sharply beyond day 1. Require larger edge
    #   for multi-day markets to compensate for added uncertainty.
    #   (days_out was computed earlier — see top of function.)
    if days_out >= 4:
        logger.debug("SignalEngine: suppressing %s — %d days out, too uncertain",
                     market.get("ticker"), days_out)
        if trading_mode: return None
        else: suppressed_reason = "too_far_out"
    horizon_penalty = 1.0 + days_out * 0.10  # +10% per day: day1=1.1x, day3=1.3x

    # 4c-iii. Z-Score / Tail Risk Penalty.
    #   A bucket 2+ standard deviations from the forecast mean requires a
    #   much larger edge to compensate for thin-tail bias in the distribution.
    z_score = 0.0
    if ensemble_spread and ensemble_mean is not None and ensemble_spread > 0:
        z_score = abs(threshold_f - ensemble_mean) / ensemble_spread
    else:
        # Fallback to the adaptive Gaussian sigma and NWS mean
        z_score = abs(threshold_f - nws_value) / sigma if 'sigma' in locals() and sigma > 0 else 0.0

    # Scale penalty continuously for tails (Z > 1.0)
    # Using an exponential curve avoids arbitrary thresholds and hard cliffs.
    # At z=1.0 -> 1.0x. At z=2.0 -> 1.6x. At z=3.0 -> 2.7x
    tail_penalty = 1.0
    if z_score > 1.0:
        tail_penalty = math.exp((z_score - 1.0) * 0.5)
        
    # Combined edge multiplier applied later.
    _edge_multiplier = spread_penalty * horizon_penalty * owm_disagreement_penalty * tail_penalty

    # 4c-iii. Time-to-expiry decay.
    #     As the market nears close, the model's edge erodes because observed
    #     temps become the ground truth.  Within 3 hours of close, blend the
    #     model toward the market's implied probability.
    #   (hours_left was computed earlier — see top of function.)
    if hours_left < 6.0:
        # Two-stage smooth decay toward market's implied probability.
        #
        # Stage 1 (3h – 6h): gentle 0% → 30% blend.
        #   The model still has most of its skill but edge is eroding.
        # Stage 2 (0h – 3h): 30% → 100% blend.
        #   Real-time observations are now the ground truth; model is stale.
        #
        # This replaces the old abrupt 3-hour linear blend and gives more
        # graceful edge erosion for signals placed during the mid-afternoon.
        if hours_left >= 3.0:
            blend_weight = (6.0 - hours_left) / 3.0 * 0.30
        else:
            blend_weight = 0.30 + (3.0 - hours_left) / 3.0 * 0.70

        model_prob: float = model_prob_raw * (1.0 - blend_weight) + implied_prob * blend_weight
        model_prob = max(0.001, min(0.999, model_prob))
        logger.debug(
            "SignalEngine: time-decay %.1fh left (blend=%.0f%%) → model_prob %.2f→%.2f for %s",
            hours_left, blend_weight * 100, model_prob_raw, model_prob, market.get("ticker"),
        )
    else:
        model_prob = model_prob_raw

    # 4c-iv. METAR real-time observation override (same-day markets only).
    #   If the current observed temperature already settles the question,
    #   override the ensemble probability with a near-certain value.
    #   (station_id was computed earlier — see top of function.)
    metar_override_active = False
    if station_id and hours_left < 18 and direction in ("above", "below"):
        constrained = metar_client.get_constrained_prob(
            station_id=station_id,
            threshold_f=threshold_f,
            direction=direction,
            hours_left=hours_left,
            metric=metric,
        )
        if constrained is not None:
            logger.info(
                "SignalEngine: METAR override for %s → model_prob=%.2f",
                market.get("ticker"), constrained,
            )
            model_prob = constrained
            metar_override_active = True

    # 4d-i. Post-nadir suppression for same-day LOW markets.
    #
    # The daily LOW temperature occurs in the early morning (4-7 AM local),
    # which is 12-20 hours before the market close time (~1 AM next day).
    # By the time the bot runs in the afternoon/evening, the morning low has
    # already happened — but neither NWS hourly (future hours only) nor the
    # current METAR observation can tell us what it was.
    #
    # Rule: if hours_left < 20 and same-day LOW, the morning trough is in the
    # past and our forecast data is unreliable.  ONLY allow the signal through
    # if METAR provided a real hard constraint — model_prob extremes that came
    # from the Gaussian-on-NWS fallback are NOT reliable here, since NWS hourly
    # for "today" excludes already-passed morning hours.
    if (
        metric == "low"
        and days_out == 0
        and hours_left < 20.0
        and not metar_override_active
    ):
        logger.debug(
            "SignalEngine: suppressing post-nadir LOW for %s "
            "(%.1fh left, daily low already occurred).",
            market.get("ticker"), hours_left,
        )
        if trading_mode: return None
        else: suppressed_reason = "post_nadir"

    # 4d-ii. Post-peak suppression for same-day HIGH markets.
    #   After ~3 PM local time the afternoon high is essentially locked in.
    #   The ensemble mean for today becomes unreliable because it reflects
    #   a full-day average, not the remaining hours.  Unless METAR already
    #   gave us a hard constraint, suppress the signal and let METAR/time-
    #   decay handle it instead.  We define "post-peak" as hours_left < 9
    #   (market closes ~7 AM UTC next day ≈ 3 AM local; 9 h before that
    #   ≈ 6 PM local, well past the typical afternoon high).
    if (
        metric == "high"
        and days_out == 0          # same-day market only
        and hours_left < 9.0
        and ensemble_spread is not None  # ensemble data was available
        and station_id             # we have a station to query
    ):
        # If METAR already gave a hard constraint we have good information;
        # otherwise the ensemble is stale and we should not trade.
        metar_constrained = metar_client.get_constrained_prob(
            station_id=station_id,
            threshold_f=threshold_f,
            direction=direction,
            hours_left=hours_left,
        )
        if metar_constrained is None:
            logger.debug(
                "SignalEngine: suppressing post-peak HIGH for %s "
                "(%.1fh left, no hard METAR constraint).",
                market.get("ticker"), hours_left,
            )
            if trading_mode: return None
            else: suppressed_reason = "post_peak"
        # METAR has a hard answer — use it (already set above, but re-apply).
        model_prob = metar_constrained

    # 5. Sanity guard: if the market is extremely one-sided (<=5c implied)
    #    our simple NWS model is almost certainly wrong — the market has
    #    real-time information (actual observed temps, updated NWS gridpoints)
    #    that we don't have.
    #    Tier 1 (<=3c): require >=92% model confidence — near-certain.
    #    Tier 2 (<=5c): require >=80% model confidence.
    if yes_ask <= 3 and model_prob < 0.92:
        logger.debug(
            "SignalEngine: suppressing <=3c YES for %s — market=%dc model=%.0f%%",
            market.get("ticker"), yes_ask, model_prob * 100,
        )
        if trading_mode: return None
        else: suppressed_reason = "sanity_guard_3c_yes"
    if yes_ask <= 5 and model_prob < 0.80:
        logger.debug(
            "SignalEngine: suppressing <=5c YES for %s — market=%dc model=%.0f%%",
            market.get("ticker"), yes_ask, model_prob * 100,
        )
        if trading_mode: return None
        else: suppressed_reason = "sanity_guard_5c_yes"
    # Mirror guards for NO side (yes_ask >= 95c means NO is <=5c).
    if yes_ask >= 97 and model_prob > 0.08:
        logger.debug(
            "SignalEngine: suppressing <=3c NO for %s — market=%dc model=%.0f%%",
            market.get("ticker"), yes_ask, model_prob * 100,
        )
        if trading_mode: return None
        else: suppressed_reason = "sanity_guard_3c_no"
    if yes_ask >= 95 and model_prob > 0.20:
        logger.debug(
            "SignalEngine: suppressing <=5c NO for %s — market=%dc model=%.0f%%",
            market.get("ticker"), yes_ask, model_prob * 100,
        )
        if trading_mode: return None
        else: suppressed_reason = "sanity_guard_5c_no"

    # 6. Edge and recommended side.
    edge: float = model_prob - implied_prob
    side: Literal["yes", "no"] = "yes" if edge > 0 else "no"

    # Apply spread and horizon penalties to the effective minimum edge threshold.
    # Signals with high forecast uncertainty require proportionally larger edge.
    
    # Benjamini-Hochberg inspired empirical False Discovery Rate correction:
    # Instead of counting every 1-degree bucket as an independent test (Bonferroni),
    # we penalize based on the number of independent physical weather regions.
    num_regions = 30  # Roughly the number of cities tracked
    empirically_calibrated_multiplier = 0.002
    spatial_penalty = empirically_calibrated_multiplier * math.log(num_regions) if num_regions > 1 else 0.0
    
    # Temporal penalty: track how many times this bucket has been evaluated today
    times_evaluated_today = 0
    eval_counts_file = os.path.join(os.path.dirname(__file__), ".eval_counts.json")
    try:
        import tempfile
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        counts_data: dict = {}
        if os.path.exists(eval_counts_file):
            try:
                with open(eval_counts_file, "r") as f:
                    counts_data = json.load(f)
            except (json.JSONDecodeError, OSError) as read_exc:
                # Corrupted or empty file — reset silently (don't crash evaluation)
                logger.warning("SignalEngine: eval_counts.json unreadable (%s) — resetting.", read_exc)
                counts_data = {}
        if counts_data.get("date") != today_str:
            counts_data = {"date": today_str, "counts": {}}
            
        ticker = market.get("ticker") or str(market.get("condition_id"))
        times_evaluated_today = counts_data["counts"].get(ticker, 0)
        
        # Increment and write back atomically (same pattern as HALT_FILE)
        counts_data["counts"][ticker] = times_evaluated_today + 1
        _eval_dir = os.path.dirname(eval_counts_file) or "."
        fd, temp_path = tempfile.mkstemp(dir=_eval_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(counts_data, f)
            os.replace(temp_path, eval_counts_file)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.debug(f"Failed to track eval counts: {exc}")
        
    temporal_penalty = 0.5 * empirically_calibrated_multiplier * math.log(1 + times_evaluated_today)
    
    effective_min_edge = (CONFIG["MIN_EDGE_THRESHOLD"] * _edge_multiplier) + spatial_penalty + temporal_penalty
    
    raw_model_prob = model_prob
    if tail_penalty > 1.0:
        # Heavily shrink the probability used for sizing to account for tail uncertainty
        model_prob = min(1.0, max(0.0, raw_model_prob / tail_penalty))
        # Recompute edge with the shrunken probability for sizing
        edge = model_prob - implied_prob

    # 7. Kelly sizing — adjusted for ensemble spread.
    #   Tight spread (< 2°F) = models agree = boost bet up to 1.25x.
    #   Wide spread  (> 4°F) = elevated uncertainty = reduce bet to 0.75x.
    spread_kelly_mult = 1.0
    if ensemble_spread is not None:
        if ensemble_spread < 2.0:
            spread_kelly_mult = 1.25
        elif ensemble_spread > 4.0:
            spread_kelly_mult = 0.75

    # Station-score Kelly multiplier — uses the accumulated win/loss history to
    # scale bet size for each city.  A city with a strong track record gets up
    # to 1.30x; a city that has been losing money is reduced to 0.50x.
    # We only deviate from neutral (1.0) once there is enough history for the
    # score to be meaningful (score drifts from 1.0 only after several trades).
    station_kelly_mult = max(0.50, min(1.30, station_score))
    
    # Apply the same tail penalty (haircut) to the edge used for Kelly sizing,
    # so we don't bet aggressively on uncertain far-tail outcomes even if they clear the threshold.
    haircut_edge = abs(edge) / tail_penalty
    
    kelly_dollars  = haircut_edge * CONFIG["KELLY_FRACTION"] * bankroll * spread_kelly_mult * station_kelly_mult
    kelly_contracts = max(1, min(int(kelly_dollars), int(bankroll * 0.08)))
    kelly_dollars   = round(kelly_dollars, 2)

    # 8. Signal strength label (based on abs_edge vs effective threshold).
    abs_edge = abs(edge)
    if abs_edge > 0.25:
        signal_strength = "strong"
    elif abs_edge > 0.15:
        signal_strength = "moderate"
    else:
        signal_strength = "weak"

    station_meta = STATION_MAP.get(city, {})

    return {
        # Market identity
        "ticker":        market.get("ticker", ""),
        "market_title":  market.get("title", ""),
        "close_time":    market.get("close_time", ""),
        "market_date":   date_str,
        # Parsed market semantics
        "city":          city,
        "metric":        metric,
        "threshold_f":   threshold_f,
        "threshold_lo":  threshold_lo,
        "threshold_hi":  threshold_hi,
        "direction":     direction,
        # Station metadata
        "station":       station_meta.get("station", ""),
        # Forecast data
        "nws_predicted": nws_value,
        "owm_predicted": round(owm_value, 1) if owm_value is not None else None,
        "source_agreement": source_agreement,
        "hours_left":    round(hours_left, 1),
        "margin_f":      round(margin, 2),
        # Ensemble metadata
        "ensemble_members": ensemble_members,
        "ensemble_mean":    round(ensemble_mean, 1) if ensemble_mean is not None else None,
        "ensemble_spread":  round(ensemble_spread, 1) if ensemble_spread is not None else None,
        "days_out":         days_out,
        # Probability model
        "implied_prob":  round(implied_prob, 4),
        "raw_model_prob":round(raw_model_prob, 4),
        "model_prob":    round(model_prob, 4),
        "edge":          round(edge, 4),
        "effective_min_edge": round(effective_min_edge, 4),
        "z_score":       round(z_score, 2),
        # Trade recommendation
        "recommended_side":  side,
        "kelly_contracts":   kelly_contracts,
        "kelly_dollars":     kelly_dollars,
        "signal_strength":   signal_strength,
    }


# ---------------------------------------------------------------------------
# Batch signal generation
# ---------------------------------------------------------------------------

def get_all_signals(markets: list[dict], bankroll: float) -> list[dict]:
    """Evaluate all markets and return actionable signals sorted by edge.

    Skips markets that:
    - Cannot be parsed or have missing price data.
    - Have |edge| < MIN_EDGE_THRESHOLD (from CONFIG).
    - Already have an open position in the bot log or a non-zero Kalshi
      exchange position (so redeploys do not re-signal held tickers).

    Deduplication:
    - For each (city, metric) pair only the single highest-|edge| signal is
      kept. This prevents contradictory positions (e.g. betting YES on both
      "NYC HIGH above 90°F" and "NYC HIGH below 83°F" in the same cycle).

    Returns:
        List of signal dicts sorted by abs(edge) descending.
    """
    min_edge: float = CONFIG["MIN_EDGE_THRESHOLD"]
    already_traded: set[str] = _load_traded_tickers()

    # Minimum open interest required to trust the market price.
    # Below this threshold the spread is usually stale and edge estimates
    # are unreliable.
    _MIN_OPEN_INTEREST_KALSHI = 50
    _MIN_OPEN_INTEREST_POLY = 1

    candidates: list[dict] = []
    skipped_calc      = 0
    skipped_edge      = 0
    skipped_duplicate = 0
    skipped_liquidity = 0

    num_comparisons = len(markets)
    logger.info(f"SignalEngine: Evaluating {num_comparisons} total markets for potential edges.")

    for market in markets:
        ticker = market.get("ticker", "")

        if ticker in already_traded:
            skipped_duplicate += 1
            logger.debug("SignalEngine: skipping already-traded ticker %s.", ticker)
            continue

        try:
            oi = float(market.get("open_interest") or 0)
        except (TypeError, ValueError):
            oi = 0.0
        min_oi = (
            _MIN_OPEN_INTEREST_POLY
            if market.get("venue") == "poly_us"
            else _MIN_OPEN_INTEREST_KALSHI
        )
        if oi < min_oi:
            skipped_liquidity += 1
            logger.debug(
                "SignalEngine: skipping illiquid market %s (OI=%s min=%s).",
                ticker,
                oi,
                min_oi,
            )
            continue

        edge_data = calculate_edge(market, bankroll, num_comparisons=num_comparisons)

        if edge_data is None:
            skipped_calc += 1
            continue

        if abs(edge_data["edge"]) < edge_data.get("effective_min_edge", min_edge):
            skipped_edge += 1
            logger.debug(
                "SignalEngine: edge %.4f below effective threshold %.2f for %s.",
                edge_data["edge"], edge_data.get("effective_min_edge", min_edge), ticker,
            )
            continue

        candidates.append(edge_data)

    # Deduplicate: keep only the best signal per (city, metric).
    best: dict[tuple, dict] = {}
    skipped_conflict = 0
    for sig in candidates:
        key = (sig["city"], sig["metric"])
        if key not in best or abs(sig["edge"]) > abs(best[key]["edge"]):
            if key in best:
                skipped_conflict += 1
            best[key] = sig
        else:
            skipped_conflict += 1

    # ── Regional Correlation Limits & Extreme Event Override ──
    final_signals = []
    region_counts = {"texas": 0, "northeast": 0, "west_coast": 0}
    
    for sig in sorted(best.values(), key=lambda s: abs(s["edge"]), reverse=True):
        city = sig["city"]
        
        # In production this would query the active NHC (National Hurricane Center) shapefiles
        # to see if the city is inside the cone of uncertainty. Here we implement the circuit breaker.
        if "hurricane" in sig.get("title", "").lower() or "tropical" in sig.get("title", "").lower():
            logger.warning(f"🚨 Extreme Event Failsafe: Skipping {city} due to active tropical threat.")
            continue
            
        if city in {"Dallas", "Houston", "Austin", "San Antonio"}:
            if region_counts["texas"] >= 2:
                logger.info(f"Regional Risk Cap: Skipping {city} to prevent overexposure to Texas correlated weather.")
                continue
            region_counts["texas"] += 1
            
        elif city in {"New York", "Boston", "Philadelphia", "Washington DC"}:
            if region_counts["northeast"] >= 2:
                logger.info(f"Regional Risk Cap: Skipping {city} to prevent overexposure to Northeast correlated weather.")
                continue
            region_counts["northeast"] += 1
            
        elif city in {"Los Angeles", "San Francisco", "Seattle"}:
            if region_counts["west_coast"] >= 2:
                logger.info(f"Regional Risk Cap: Skipping {city} to prevent overexposure to West Coast correlated weather.")
                continue
            region_counts["west_coast"] += 1
            
        final_signals.append(sig)

    logger.info(
        "SignalEngine: %d markets → %d signals "
        "(skipped: %d illiquid, %d calc, %d edge, %d duplicate, %d conflict, %d regional caps).",
        len(markets),
        len(final_signals),
        skipped_liquidity,
        skipped_calc,
        skipped_edge,
        skipped_duplicate,
        skipped_conflict,
        len(best) - len(final_signals)
    )
    return final_signals


# ---------------------------------------------------------------------------
# Auto-bet eligibility filter
# ---------------------------------------------------------------------------

def should_auto_bet(signal: dict) -> tuple[bool, str]:
    """Decide whether a signal qualifies for autonomous placement.

    Conservative: every check below must pass. Returns (ok, reason) where
    *reason* explains why the signal was rejected (or "ok" on success).

    Criteria (all must hold):
      • Edge ≥ AUTO_BET_MIN_EDGE                      (default 5¢ — any profit)
      • model_prob ≥ AUTO_BET_MIN_PROB_YES (YES side)  (default 85%)
      • model_prob ≤ AUTO_BET_MAX_PROB_NO  (NO side)   (default 15%)
      • Ensemble spread ≤ AUTO_BET_MAX_SPREAD          (default 2.5°F)
      • |forecast − threshold| ≥ AUTO_BET_MIN_MARGIN   (default 2.0°F)
      • Ensemble data present (≥10 members)
      • Days out ≤ AUTO_BET_MAX_HORIZON_DAYS           (default 1)
      • hours_left in [6, 60]
    """
    min_edge   = float(CONFIG.get("AUTO_BET_MIN_EDGE")         or 0.05)
    max_spread = float(CONFIG.get("AUTO_BET_MAX_SPREAD")       or 2.5)
    min_margin = float(CONFIG.get("AUTO_BET_MIN_MARGIN")       or 2.0)
    max_days   = int(CONFIG.get("AUTO_BET_MAX_HORIZON_DAYS")   or 1)

    edge = abs(float(signal.get("edge", 0.0)))
    if edge < min_edge:
        return False, f"edge {edge:.2f} < {min_edge:.2f}"

    margin = float(signal.get("margin_f", 0.0))
    if margin < min_margin:
        return False, f"margin {margin:.1f}°F < {min_margin:.1f}°F"

    ens_members = int(signal.get("ensemble_members") or 0)
    ens_spread  = signal.get("ensemble_spread")
    if ens_members < 10 or ens_spread is None:
        return False, "no ensemble data"
    if float(ens_spread) > max_spread:
        return False, f"spread {ens_spread:.1f}°F > {max_spread:.1f}°F"

    days_out = int(signal.get("days_out", 0))
    if days_out > max_days:
        return False, f"days_out {days_out} > {max_days}"

    model_prob   = float(signal.get("model_prob", 0.5))
    side         = signal.get("recommended_side", "yes")
    min_prob_yes = float(CONFIG.get("AUTO_BET_MIN_PROB_YES") or 0.85)
    max_prob_no  = float(CONFIG.get("AUTO_BET_MAX_PROB_NO")  or 0.15)
    if side == "yes" and model_prob < min_prob_yes:
        return False, f"YES model_prob {model_prob:.2f} < {min_prob_yes:.2f}"
    if side == "no"  and model_prob > max_prob_no:
        return False, f"NO model_prob {model_prob:.2f} > {max_prob_no:.2f}"

    hours_left = float(signal.get("hours_left", 999.0))
    if hours_left < 6.0:
        return False, f"hours_left {hours_left:.1f} < 6.0 (too close to close)"
    if hours_left > 60.0:
        return False, f"hours_left {hours_left:.1f} > 60.0 (too far out)"

    return True, "ok"
