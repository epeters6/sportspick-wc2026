"""
Polymarket MLB moneyline parsing + signal assembly with ``mlb_signal_engine``.
"""

from __future__ import annotations

import logging
import json
import re
from datetime import date
from typing import Any

from config import CONFIG
from pipeline import mlb_signal_engine

logger = logging.getLogger(__name__)

# nickname / phrase (lower) -> team abbreviation
TEAM_ALIASES: dict[str, str] = {
    "arizona diamondbacks": "ARI",
    "diamondbacks": "ARI",
    "d-backs": "ARI",
    "dbacks": "ARI",
    "atlanta braves": "ATL",
    "braves": "ATL",
    "baltimore orioles": "BAL",
    "orioles": "BAL",
    "boston red sox": "BOS",
    "red sox": "BOS",
    "chicago cubs": "CHC",
    "cubs": "CHC",
    "chicago white sox": "CHW",
    "white sox": "CHW",
    "cincinnati reds": "CIN",
    "reds": "CIN",
    "cleveland guardians": "CLE",
    "guardians": "CLE",
    "indians": "CLE",
    "colorado rockies": "COL",
    "rockies": "COL",
    "detroit tigers": "DET",
    "tigers": "DET",
    "houston astros": "HOU",
    "astros": "HOU",
    "kansas city royals": "KCR",
    "royals": "KCR",
    "los angeles angels": "LAA",
    "angels": "LAA",
    "los angeles dodgers": "LAD",
    "dodgers": "LAD",
    "miami marlins": "MIA",
    "marlins": "MIA",
    "milwaukee brewers": "MIL",
    "brewers": "MIL",
    "minnesota twins": "MIN",
    "twins": "MIN",
    "new york mets": "NYM",
    "mets": "NYM",
    "new york yankees": "NYY",
    "yankees": "NYY",
    "athletics": "OAK",
    "oakland athletics": "OAK",
    "philadelphia phillies": "PHI",
    "phillies": "PHI",
    "pittsburgh pirates": "PIT",
    "pirates": "PIT",
    "san diego padres": "SDP",
    "padres": "SDP",
    "san francisco giants": "SFG",
    "giants": "SFG",
    "seattle mariners": "SEA",
    "mariners": "SEA",
    "st louis cardinals": "STL",
    "st. louis cardinals": "STL",
    "cardinals": "STL",
    "tampa bay rays": "TBR",
    "rays": "TBR",
    "texas rangers": "TEX",
    "rangers": "TEX",
    "toronto blue jays": "TOR",
    "blue jays": "TOR",
    "washington nationals": "WSN",
    "nationals": "WSN",
}

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def model_confidence_from_breakdown(prob_result: dict[str, Any]) -> float:
    """0–1 — blend of component agreement + model decisiveness vs 50/50.

    Uses the new ``*_edge`` fields (centered at 0) when present. Component
    agreement is the share of edges pointing the same direction as the net
    ``raw_edge`` — i.e. if pitcher, bullpen, and form all favor home, that's
    high agreement. Decisiveness scales with ``|final_home_prob − 0.50|``.
    """
    edge_keys = ("pitcher_edge", "bullpen_edge", "form_edge", "lineup_edge", "travel_edge")
    edges: list[float] = []
    for k in edge_keys:
        v = prob_result.get(k)
        if v is None:
            continue
        try:
            edges.append(float(v))
        except (TypeError, ValueError):
            continue

    fh = float(prob_result.get("final_home_prob") or 0.5)
    decisiveness = _clamp(abs(fh - 0.5) * 2.4, 0.0, 1.0)

    if not edges:
        # Fall back to legacy ``*_prob`` view if breakdown is from old runs.
        parts: list[float] = []
        for k in ("pitcher_prob", "bullpen_prob", "form_prob", "lineup_prob", "travel_prob"):
            v = prob_result.get(k)
            if v is None:
                continue
            try:
                parts.append(float(v))
            except (TypeError, ValueError):
                continue
        if len(parts) < 3:
            return float(_clamp(decisiveness, 0.05, 0.95))
        m = sum(parts) / len(parts)
        var = sum((x - m) ** 2 for x in parts) / len(parts)
        span = max(parts) - min(parts)
        legacy = 0.55 * _clamp(1.0 - var * 35.0, 0.0, 1.0) + 0.45 * _clamp(span / 0.25, 0.0, 1.0)
        return float(_clamp(0.45 * legacy + 0.55 * decisiveness, 0.05, 0.95))

    raw_edge = sum(edges)
    if abs(raw_edge) < 1e-6:
        agreement = 0.0
    else:
        # Fraction of edges aligned with the net direction, weighted by magnitude.
        net_sign = 1.0 if raw_edge > 0 else -1.0
        aligned = sum(abs(e) for e in edges if (e * net_sign) > 0)
        total = sum(abs(e) for e in edges)
        agreement = aligned / total if total > 1e-6 else 0.0

    # Magnitude of the net edge — capped at ~0.20 (very strong matchup).
    magnitude = _clamp(abs(raw_edge) / 0.18, 0.0, 1.0)

    combined = 0.40 * agreement + 0.30 * magnitude + 0.30 * decisiveness
    return float(_clamp(combined, 0.05, 0.95))


def _abbr_from_token(text: str) -> str | None:
    t = text.lower().strip()
    if not t:
        return None
    # Longest alias first
    for alias in sorted(TEAM_ALIASES.keys(), key=len, reverse=True):
        if alias in t:
            return TEAM_ALIASES[alias]
    if t.upper() in {"ARI", "ATL", "BAL", "BOS", "CHC", "CHW", "CIN", "CLE", "COL", "DET", "HOU", "KCR", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "OAK", "PHI", "PIT", "SDP", "SFG", "SEA", "STL", "TBR", "TEX", "TOR", "WSN", "AZ", "TB", "KC", "SD", "SF", "ATH", "WSH"}:
        m = {
            "AZ": "ARI",
            "TB": "TBR",
            "KC": "KCR",
            "SD": "SDP",
            "SF": "SFG",
            "ATH": "OAK",
            "WSH": "WSN",
        }
        return m.get(t.upper(), t.upper())
    return None


def _extract_game_date(title: str) -> str | None:
    iso = re.search(r"(20\d{2})-(\d{2})-(\d{2})", title)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    mdy = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b",
        title,
        re.I,
    )
    if mdy:
        mo = _MONTHS[mdy.group(1).lower()]
        d_ = int(mdy.group(2))
        y_ = int(mdy.group(3))
        return date(y_, mo, d_).isoformat()
    return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _normalize_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price * 100.0 if price <= 1.0 else price


def _yes_price(market: dict[str, Any]) -> float | None:
    for k in ("yes_ask", "yes_price", "yesPrice"):
        v = market.get(k)
        if v is not None:
            price = _normalize_price(v)
            if price is not None:
                return price

    outcomes = _as_list(market.get("outcomes"))
    prices = _as_list(market.get("outcomePrices"))
    if outcomes and prices and len(outcomes) == len(prices):
        for outcome, price_raw in zip(outcomes, prices):
            if str(outcome).strip().lower() == "yes":
                return _normalize_price(price_raw)

    # If the row does not tell us which outcome is YES, skip it rather than
    # assuming index 0. Polymarket rows are not guaranteed to order outcomes
    # as ["Yes", "No"], and assuming that created fake 96-99c YES prices.
    return None


def parse_mlb_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """
    Parse a moneyline-style Polymarket row. Skips totals (over/under).

    Returns ``favored_team`` as **abbreviation** when possible.
    """
    title = (market.get("question") or market.get("title") or "").strip()
    if not title:
        return None
    low = title.lower()
    if "over" in low or "under" in low:
        return None

    gdate = _extract_game_date(title)
    favored: str | None = None

    m_win = re.search(
        r"Will (?:the )?(.+?) win\b",
        title,
        re.I,
    )
    m_beat = re.search(
        r"(?:Will )?(?:the )?(.+?) beat (?:the )?(.+?)(?:\?|$)",
        title,
        re.I,
    )
    if m_win:
        favored = _abbr_from_token(m_win.group(1))
    elif m_beat:
        favored = _abbr_from_token(m_beat.group(1))
    else:
        for alias in sorted(TEAM_ALIASES.keys(), key=len, reverse=True):
            if alias in low:
                favored = TEAM_ALIASES[alias]
                break

    if not favored:
        return None

    yp = _yes_price(market)
    if yp is None:
        return None

    ticker = (
        market.get("ticker")
        or market.get("slug")
        or market.get("id")
        or market.get("conditionId")
        or ""
    )

    return {
        "favored_team": favored,
        "game_date": gdate,
        "ticker": str(ticker),
        "yes_price": round(yp, 2),
        "title": title,
    }


def _game_abbr(game: dict[str, Any]) -> tuple[str | None, str | None]:
    h = game.get("home_team") or {}
    a = game.get("away_team") or {}
    ha = h.get("abbr") or h.get("abbreviation")
    aa = a.get("abbr") or a.get("abbreviation")
    fix = {
        "AZ": "ARI",
        "TB": "TBR",
        "KC": "KCR",
        "SD": "SDP",
        "SF": "SFG",
        "WSH": "WSN",
        "CWS": "CHW",
        "ATH": "OAK",
    }
    if ha:
        ha = str(ha).upper()
        ha = fix.get(ha, ha)
    if aa:
        aa = str(aa).upper()
        aa = fix.get(aa, aa)
    return ha, aa


def _same_day(d1: str | None, d2: str | None) -> bool:
    if not d1 or not d2:
        return True
    return str(d1)[:10] == str(d2)[:10]


def get_all_signals(
    games: list[dict[str, Any]],
    polymarket_markets: list[dict[str, Any]],
    bankroll: float,
) -> list[dict[str, Any]]:
    min_edge = float(CONFIG.get("MIN_EDGE_THRESHOLD") or 0.12)
    # Allow an MLB-specific kelly knob, otherwise damp the global KELLY_FRACTION.
    mlb_kelly_raw = float(CONFIG.get("MLB_KELLY_FRACTION") or 0.0)
    if mlb_kelly_raw > 0:
        kelly_frac = mlb_kelly_raw
    else:
        kelly_frac = float(CONFIG.get("KELLY_FRACTION") or 0.25) * 0.5
    kelly_frac = max(0.02, min(kelly_frac, 0.35))

    # Cap any single bet to a small fraction of bankroll (default 4%).
    max_cap_frac = max(0.005, min(float(CONFIG.get("MLB_MAX_BET_BANKROLL_FRAC") or 0.04), 0.10))

    # Skip extreme-tail markets unless the model has very strong conviction. With
    # implied probability ≥ this threshold (or ≤ 1−threshold for the other
    # side), the model would need ``min_extreme_edge`` AND high confidence to
    # justify fading.
    extreme_implied = max(0.80, min(float(CONFIG.get("MLB_EXTREME_IMPLIED") or 0.92), 0.99))
    min_extreme_edge = max(0.10, min(float(CONFIG.get("MLB_EXTREME_MIN_EDGE") or 0.25), 0.50))
    min_extreme_conf = max(0.40, min(float(CONFIG.get("MLB_EXTREME_MIN_CONFIDENCE") or 0.70), 0.95))

    parsed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for m in polymarket_markets:
        p = parse_mlb_market(m)
        if p:
            parsed.append((m, p))

    out: list[dict[str, Any]] = []

    for game in games:
        prob_result = mlb_signal_engine.calculate_win_probability(game, bankroll)
        if not prob_result:
            continue
        model_home = float(prob_result["final_home_prob"])
        home_abbr, away_abbr = _game_abbr(game)
        gdate = game.get("game_date")

        candidates: list[dict[str, Any]] = []
        mconf = round(model_confidence_from_breakdown(prob_result), 4)
        travel_a = prob_result.get("travel_away") or {}
        travel_line = ""
        if away_abbr and isinstance(travel_a, dict):
            travel_line = (
                f"{away_abbr} traveling {travel_a.get('miles', 0)}mi "
                f"{travel_a.get('direction', 'n/a')} · {travel_a.get('label', '')}"
            )
        re_base = prob_result.get("run_environment") or {}
        run_f = float(re_base.get("total_factor") or re_base.get("run_factor") or 1.0)
        cond = str(re_base.get("conditions") or "—")
        hb = prob_result.get("home_bullpen") or {}
        ab = prob_result.get("away_bullpen") or {}
        mh = game.get("home") or {}
        ma = game.get("away") or {}
        hpid = (mh.get("id") if isinstance(mh, dict) else None) or (
            game.get("home_pitcher") or {}
        ).get("id")
        apid = (ma.get("id") if isinstance(ma, dict) else None) or (
            game.get("away_pitcher") or {}
        ).get("id")

        for _raw_m, pm in parsed:
            if not _same_day(pm.get("game_date"), gdate):
                continue
            fav = pm["favored_team"]
            if fav not in (home_abbr, away_abbr):
                continue

            yes_is_home = fav == home_abbr
            model_yes = model_home if yes_is_home else (1.0 - model_home)
            implied = float(pm["yes_price"]) / 100.0
            edge = model_yes - implied

            if abs(edge) < min_edge:
                continue
            is_coors = "Coors" in str(game.get("venue_name") or "")
            if is_coors and abs(edge) < 0.18:
                continue

            extreme_market = implied >= extreme_implied or implied <= (1.0 - extreme_implied)

            kelly = abs(edge) * kelly_frac * float(bankroll)
            cap_dollars = max(1.0, float(bankroll) * max_cap_frac)
            kelly = min(kelly, cap_dollars)
            contracts = max(1, int(kelly))

            # Signal strength combines edge magnitude with model confidence so a
            # large |edge| produced by a low-conviction near-50/50 model isn't
            # labeled STRONG. Confidence below 0.55 caps the label at "mild";
            # below 0.65 caps at "moderate".
            abs_edge = abs(edge)
            if abs_edge > 0.22 and mconf >= 0.65:
                strength = "strong"
            elif abs_edge > 0.12 and mconf >= 0.55:
                strength = "moderate"
            else:
                strength = "mild"
            if extreme_market and (
                abs_edge < min_extreme_edge or mconf < min_extreme_conf
            ):
                # Still post the alert for visibility/learning, but never label
                # a low-confidence 1-8 cent tail fade as STRONG. Autobet has a
                # separate implied-price band that blocks these completely.
                strength = "mild"

            candidates.append(
                {
                    "venue": "mlb_poly",
                    "game_id": game.get("game_id"),
                    "ticker": pm["ticker"],
                    "title": pm["title"],
                    "market_title": pm["title"],
                    "favored_team": fav,
                    "yes_is_home": yes_is_home,
                    "game_date": gdate,
                    "market_date": gdate,
                    "home_team_abbr": home_abbr,
                    "away_team_abbr": away_abbr,
                    "venue_name": game.get("venue_name") or "",
                    "game_time_et": game.get("game_time_et") or "",
                    "home_pitcher_id": int(hpid) if hpid else None,
                    "away_pitcher_id": int(apid) if apid else None,
                    "home_pitcher_name": (game.get("home_pitcher") or {}).get("name")
                    if isinstance(game.get("home_pitcher"), dict)
                    else None,
                    "away_pitcher_name": (game.get("away_pitcher") or {}).get("name")
                    if isinstance(game.get("away_pitcher"), dict)
                    else None,
                    "model_home_prob": round(model_home, 4),
                    "model_yes_prob": round(model_yes, 4),
                    "model_prob": round(model_yes, 4),
                    "predicted_home_win": model_home > 0.5,
                    "yes_price": pm["yes_price"],
                    "implied_prob": round(implied, 4),
                    "edge": round(edge, 4),
                    "signal_strength": strength,
                    "model_confidence": mconf,
                    "kelly_dollars": round(kelly, 2),
                    "kelly_contracts": contracts,
                    "recommended_side": "yes" if edge > 0 else "no",
                    "extreme_market": extreme_market,
                    "autobet_block_reason": "extreme_implied" if extreme_market else "",
                    "probability_breakdown": prob_result,
                    "park_run_factor": round(run_f, 4),
                    "park_conditions": cond,
                    "travel_summary": travel_line,
                    "home_bullpen_label": hb.get("label", "—"),
                    "away_bullpen_label": ab.get("label", "—"),
                    "focus_pitcher_name": (
                        (game.get("home_pitcher") or {}).get("name")
                        if yes_is_home
                        else (game.get("away_pitcher") or {}).get("name")
                    ),
                }
            )

        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda s: (
                abs(float(s.get("edge") or 0)),
                float(s.get("model_confidence") or 0),
                str(s.get("ticker") or ""),
            ),
        )
        if len(candidates) > 1:
            logger.debug(
                "polymarket_mlb_parser: picked 1 of %d Polymarket rows for game_id=%s (%s @ %s)",
                len(candidates),
                game.get("game_id"),
                away_abbr,
                home_abbr,
            )
        out.append(best)

    out.sort(
        key=lambda s: (
            float(s.get("model_confidence") or 0),
            abs(float(s.get("edge") or 0)),
        ),
        reverse=True,
    )
    logger.info(
        "polymarket_mlb_parser: %d signals (min_edge=%.3f, games=%d, markets=%d)",
        len(out),
        min_edge,
        len(games),
        len(polymarket_markets),
    )
    return out


# Back-compat names
parse_polymarket_mlb_row = parse_mlb_market


def looks_like_mlb_game_market(text: str) -> bool:
    t = (text or "").lower()
    if not t.strip():
        return False
    if "over" in t or "under" in t:
        return False
    if "mlb" in t or "baseball" in t:
        return True
    return bool(re.search(r"\b(beat|vs\.?|@\s)\b", t, re.I))
