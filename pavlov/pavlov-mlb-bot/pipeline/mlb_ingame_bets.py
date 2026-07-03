"""In-game MLB moneyline candidates: large lead + late inning + Polymarket still shy of ~certain."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import CONFIG
from pipeline import mlb_client, mlb_ingame_learning
from pipeline.polymarket_mlb_parser import _game_abbr, _same_day, parse_mlb_market
from polymarket import paths as poly_paths

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _is_pregame_auto_placed_via(pv: str) -> bool:
    p = (pv or "").lower()
    return "auto_mlb" in p and "ingame" not in p


def _is_ingame_auto_placed_via(pv: str) -> bool:
    return "ingame" in (pv or "").lower()


def game_has_ingame_auto_today(
    positions: list,
    game_id: int,
) -> bool:
    today = datetime.now(_ET).date()
    for p in positions:
        if not isinstance(p, dict) or p.get("venue") != "mlb_poly":
            continue
        if int(p.get("game_id") or 0) != int(game_id):
            continue
        if not _is_ingame_auto_placed_via(str(p.get("placed_via", ""))):
            continue
        pa = p.get("placed_at")
        if not pa:
            continue
        try:
            dt = datetime.fromisoformat(str(pa).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(_ET).date() == today:
                return True
        except (ValueError, TypeError):
            continue
    return False


def has_open_ingame_auto(positions: list, game_id: int) -> bool:
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("venue") != "mlb_poly" or p.get("status") != "open":
            continue
        if int(p.get("game_id") or 0) != int(game_id):
            continue
        if _is_ingame_auto_placed_via(str(p.get("placed_via", ""))):
            return True
    return False


def game_has_pregame_auto_today(positions: list, game_id: int) -> bool:
    today = datetime.now(_ET).date()
    for p in positions:
        if not isinstance(p, dict) or p.get("venue") != "mlb_poly":
            continue
        if int(p.get("game_id") or 0) != int(game_id):
            continue
        if not _is_pregame_auto_placed_via(str(p.get("placed_via", ""))):
            continue
        pa = p.get("placed_at")
        if not pa:
            continue
        try:
            dt = datetime.fromisoformat(str(pa).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(_ET).date() == today:
                return True
        except (ValueError, TypeError):
            continue
    return False


def has_open_pregame_auto(positions: list, game_id: int) -> bool:
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("venue") != "mlb_poly" or p.get("status") != "open":
            continue
        if int(p.get("game_id") or 0) != int(game_id):
            continue
        if _is_pregame_auto_placed_via(str(p.get("placed_via", ""))):
            return True
    return False


def count_pregame_autos_today_et(positions: list) -> int:
    today = datetime.now(_ET).date()
    n = 0
    for p in positions:
        if not isinstance(p, dict) or p.get("venue") != "mlb_poly":
            continue
        if not _is_pregame_auto_placed_via(str(p.get("placed_via", ""))):
            continue
        pa = p.get("placed_at")
        if not pa:
            continue
        try:
            dt = datetime.fromisoformat(str(pa).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(_ET).date() == today:
                n += 1
        except (ValueError, TypeError):
            continue
    return n


def count_ingame_autos_today_et(positions: list) -> int:
    today = datetime.now(_ET).date()
    n = 0
    for p in positions:
        if not isinstance(p, dict) or p.get("venue") != "mlb_poly":
            continue
        if not _is_ingame_auto_placed_via(str(p.get("placed_via", ""))):
            continue
        pa = p.get("placed_at")
        if not pa:
            continue
        try:
            dt = datetime.fromisoformat(str(pa).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(_ET).date() == today:
                n += 1
        except (ValueError, TypeError):
            continue
    return n


def _load_positions() -> list:
    try:
        with open(poly_paths.POSITIONS, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def build_ingame_signals(
    games: list[dict],
    polymarket_markets: list[dict],
) -> list[dict]:
    """All passing live gates **sorted best-first** (run diff, then inning).

    Respects per-game and daily caps using ``positions.json`` snapshot.
    """
    positions = _load_positions()
    max_day = int(CONFIG.get("POLY_MLB_INGAME_MAX_BETS_PER_ET_DAY") or 5)
    if count_ingame_autos_today_et(positions) >= max_day:
        logger.info(
            "mlb_ingame_bets: daily in-game cap %d reached",
            max_day,
        )
        return []

    parsed: list[tuple[dict, dict]] = []
    for m in polymarket_markets:
        p = parse_mlb_market(m)
        if p:
            parsed.append((m, p))

    th = mlb_ingame_learning.get_thresholds()
    candidates: list[dict] = []

    for game in games:
        gid = game.get("game_id")
        if gid is None:
            continue
        if game_has_ingame_auto_today(positions, int(gid)):
            continue
        if has_open_ingame_auto(positions, int(gid)):
            continue

        snap = mlb_client.get_live_game_snapshot(int(gid))
        if not snap:
            continue
        hr = int(snap["home_runs"])
        ar = int(snap["away_runs"])
        if hr == ar:
            continue
        run_diff = abs(hr - ar)
        inning = int(snap["inning"])
        if run_diff + 1e-9 < float(th["min_run_diff"]):
            continue
        if inning + 1e-9 < float(th["min_inning"]):
            continue

        home_abbr, away_abbr = _game_abbr(game)
        if not home_abbr or not away_abbr:
            continue
        gdate = game.get("game_date")

        matched_pm = None
        matched_raw = None
        for raw_m, pm in parsed:
            if not _same_day(pm.get("game_date"), str(gdate) if gdate else None):
                continue
            fav = pm["favored_team"]
            if fav not in (home_abbr, away_abbr):
                continue
            matched_pm = pm
            matched_raw = raw_m
            break
        if matched_pm is None or matched_raw is None:
            continue

        yes_is_home = matched_pm["favored_team"] == home_abbr
        implied = float(matched_pm["yes_price"]) / 100.0

        home_ahead = hr > ar
        if home_ahead:
            recommended_side = "yes" if yes_is_home else "no"
        else:
            recommended_side = "no" if yes_is_home else "yes"

        if recommended_side == "yes":
            imp_side = implied
        else:
            imp_side = 1.0 - implied

        if imp_side + 1e-9 < float(th["min_implied_yes"]):
            continue
        if imp_side > float(th["max_implied_yes"]) + 1e-9:
            continue

        bump_rd = 0.045 * min(run_diff, 10)
        bump_inn = 0.015 * max(0, inning - 5)
        if home_ahead:
            model_home = 0.5 + bump_rd + bump_inn
        else:
            model_home = 0.5 - bump_rd - bump_inn
        model_home = max(0.08, min(0.92, model_home))

        max_k = float(CONFIG.get("POLY_MLB_INGAME_MAX_KELLY_DOLLARS") or 2.0)
        max_c = int(CONFIG.get("POLY_MLB_INGAME_MAX_CONTRACTS") or 1)

        unit = implied if recommended_side == "yes" else (1.0 - implied)
        if unit <= 0:
            continue
        contracts = max(1, min(max_c, int(max_k / unit)))

        est = contracts * unit
        kelly_dollars = round(min(max_k, est), 2)
        if kelly_dollars <= 0:
            continue

        hp = game.get("home_pitcher") or {}
        ap = game.get("away_pitcher") or {}
        if not isinstance(hp, dict):
            hp = {}
        if not isinstance(ap, dict):
            ap = {}

        ingame_context = {
            "run_diff": run_diff,
            "inning": inning,
            "is_top_inning": bool(snap.get("is_top_inning")),
            "home_runs": hr,
            "away_runs": ar,
            "implied_yes": implied,
            "implied_side": round(imp_side, 4),
            "recommended_side": recommended_side,
            "thresholds_snapshot": {k: float(v) for k, v in th.items()},
        }

        model_yes = float(model_home) if yes_is_home else (1.0 - float(model_home))
        if recommended_side == "yes":
            edge = float(model_yes) - implied
        else:
            edge = (1.0 - float(model_yes)) - (1.0 - implied)
        mconf = round(min(0.5 + 0.035 * float(run_diff), 0.88), 4)

        candidates.append(
            {
                "venue": "mlb_poly",
                "game_id": gid,
                "ticker": matched_pm["ticker"],
                "title": matched_pm["title"],
                "market_title": matched_pm["title"],
                "favored_team": matched_pm["favored_team"],
                "yes_is_home": yes_is_home,
                "game_date": gdate,
                "market_date": gdate,
                "away_team_abbr": away_abbr,
                "home_team_abbr": home_abbr,
                "venue_name": game.get("venue_name") or "",
                "game_time_et": game.get("game_time_et") or "",
                "home_pitcher_id": int(hp["id"]) if hp.get("id") else None,
                "away_pitcher_id": int(ap["id"]) if ap.get("id") else None,
                "home_pitcher_name": hp.get("name"),
                "away_pitcher_name": ap.get("name"),
                "model_home_prob": round(float(model_home), 4),
                "model_yes_prob": round(model_yes, 4),
                "model_prob": round(model_yes, 4),
                "predicted_home_win": float(model_home) > 0.5,
                "yes_price": matched_pm["yes_price"],
                "implied_prob": round(implied, 4),
                "edge": round(edge, 4),
                "signal_strength": "ingame",
                "model_confidence": mconf,
                "kelly_dollars": kelly_dollars,
                "kelly_contracts": contracts,
                "recommended_side": recommended_side,
                "ingame_context": ingame_context,
                "probability_breakdown": {},
                "park_run_factor": 1.0,
                "_sort_run_diff": run_diff,
                "_sort_inning": inning,
            }
        )

    candidates.sort(
        key=lambda s: (int(s["_sort_run_diff"]), int(s["_sort_inning"])),
        reverse=True,
    )
    for s in candidates:
        s.pop("_sort_run_diff", None)
        s.pop("_sort_inning", None)
    return candidates
