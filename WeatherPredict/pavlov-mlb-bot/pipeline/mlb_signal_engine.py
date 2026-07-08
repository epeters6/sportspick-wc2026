"""
MLB win-probability stack → Polymarket edges (weights across pitcher, bullpen,
form, lineup, park baseline, travel).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from pipeline import mlb_client, park_factors, pitcher_analyzer, travel_calculator
from pipeline.bullpen_tracker import analyze_bullpen

logger = logging.getLogger(__name__)

WEIGHT_PITCHER = 0.35
WEIGHT_BULLPEN = 0.20
WEIGHT_FORM = 0.15
WEIGHT_LINEUP = 0.15
WEIGHT_PARK = 0.10
WEIGHT_TRAVEL = 0.05


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _get(g: Any, key: str, default: Any = None) -> Any:
    if isinstance(g, Mapping):
        return g.get(key, default)
    return getattr(g, key, default)


def _team_id(team: Any) -> int:
    t = _get(team, "id")
    return int(t)


def _pitcher_id(p: Any) -> int | None:
    if p is None:
        return None
    pid = _get(p, "id")
    return int(pid) if pid is not None else None


def _pitcher_throws(p: Any) -> str:
    if p is None:
        return "R"
    return str(_get(p, "throws") or "R")


def schedule_row_to_game(row: dict[str, Any]) -> dict[str, Any]:
    """Map ``mlb_client.get_todays_games()`` row to ``calculate_win_probability`` input."""
    gd = ""
    et = row.get("game_time_et") or ""
    if len(et) >= 10:
        gd = et[:10]
    park = park_factors.get_park(row.get("venue_name"))
    return {
        "game_id": row.get("game_id"),
        "home_team": row["home"],
        "away_team": row["away"],
        "home_pitcher": row.get("home_pitcher"),
        "away_pitcher": row.get("away_pitcher"),
        "venue_name": row.get("venue_name") or "",
        "game_date": gd,
        "is_dome": bool(park.get("dome")),
    }


def _opposing_split_avg(pitcher_analysis: dict[str, Any]) -> float | None:
    """Opponent team AVG vs pitcher's handedness."""
    throws = pitcher_analysis.get("throws") or "R"
    splits = pitcher_analysis.get("opp_batting_splits") or {}
    if str(throws).upper().startswith("L"):
        return splits.get("vs_lhp_avg")
    return splits.get("vs_rhp_avg")


def calculate_win_probability(game: Mapping[str, Any], bankroll: float) -> dict[str, Any] | None:
    """
    Home-win probability blend. Returns ``None`` if probables missing or short rest (≤2 days).

    ``game`` may be a dict or object with ``home_team``, ``away_team``, ``home_pitcher``,
    ``away_pitcher``, ``venue_name``, ``game_date``, ``is_dome``.
    """
    _ = bankroll

    hpitch = _get(game, "home_pitcher")
    apitch = _get(game, "away_pitcher")
    if _pitcher_id(hpitch) is None or _pitcher_id(apitch) is None:
        return None

    home = _get(game, "home_team")
    away = _get(game, "away_team")
    hid, aid = _team_id(home), _team_id(away)
    gdate = _get(game, "game_date")
    venue = str(_get(game, "venue_name") or "")
    is_dome = bool(_get(game, "is_dome"))

    h_throws = _pitcher_throws(hpitch)
    a_throws = _pitcher_throws(apitch)

    hp = pitcher_analyzer.analyze_pitcher(_pitcher_id(hpitch), aid, h_throws, gdate)
    ap = pitcher_analyzer.analyze_pitcher(_pitcher_id(apitch), hid, a_throws, gdate)

    for label, rec in (("home", hp), ("away", ap)):
        dr = rec.get("days_rest")
        if dr is not None and dr <= 2:
            logger.debug(
                "mlb_signal_engine: skip game — %s starter short rest (%s)",
                label,
                dr,
            )
            return None

    pitcher_prob = pitcher_analyzer.calculate_pitcher_matchup_prob(hp, ap)

    hb = analyze_bullpen(hid)
    ab = analyze_bullpen(aid)
    bullpen_diff = (float(hb["strength"]) - float(ab["strength"])) / 100.0
    bullpen_prob = _clamp(0.54 + bullpen_diff * 0.15, 0.35, 0.72)

    hf = mlb_client.get_team_last_n_games(hid, 10)
    af = mlb_client.get_team_last_n_games(aid, 10)
    h_rs = float(hf.get("avg_runs_scored") or 4.5)
    a_rs = float(af.get("avg_runs_scored") or 4.5)
    form_diff = (h_rs - a_rs) / 6.0
    form_prob = _clamp(0.54 + form_diff * 0.08, 0.38, 0.70)

    lineup_prob = 0.54
    hp_opp = _opposing_split_avg(hp)
    ap_opp = _opposing_split_avg(ap)
    if hp_opp is not None and hp_opp < 0.225:
        lineup_prob += 0.05
    if hp_opp is not None and hp_opp > 0.270:
        lineup_prob -= 0.05
    if ap_opp is not None and ap_opp < 0.225:
        lineup_prob -= 0.05

    run_env = park_factors.get_run_environment(venue, gdate, is_dome)

    ht = travel_calculator.calculate_fatigue(hid, gdate, True)
    at = travel_calculator.calculate_fatigue(aid, gdate, False)
    travel_prob = _clamp(
        0.54 + float(ht["fatigue_penalty"]) - float(at["fatigue_penalty"]),
        0.40,
        0.68,
    )

    raw = (
        pitcher_prob * WEIGHT_PITCHER
        + bullpen_prob * WEIGHT_BULLPEN
        + form_prob * WEIGHT_FORM
        + lineup_prob * WEIGHT_LINEUP
        + 0.54 * WEIGHT_PARK
        + travel_prob * WEIGHT_TRAVEL
    )

    coors_penalty = 0.03 if "Coors" in venue else 0.0
    final_home_prob = _clamp(raw - coors_penalty, 0.28, 0.78)

    return {
        "pitcher_prob": round(pitcher_prob, 4),
        "bullpen_prob": round(bullpen_prob, 4),
        "form_prob": round(form_prob, 4),
        "lineup_prob": round(lineup_prob, 4),
        "park_baseline_prob": 0.54,
        "travel_prob": round(travel_prob, 4),
        "weights": {
            "pitcher": WEIGHT_PITCHER,
            "bullpen": WEIGHT_BULLPEN,
            "form": WEIGHT_FORM,
            "lineup": WEIGHT_LINEUP,
            "park": WEIGHT_PARK,
            "travel": WEIGHT_TRAVEL,
        },
        "home_pitcher_analysis": hp,
        "away_pitcher_analysis": ap,
        "home_bullpen": hb,
        "away_bullpen": ab,
        "home_form": hf,
        "away_form": af,
        "home_batting_splits": mlb_client.get_team_batting_splits(hid),
        "away_batting_splits": mlb_client.get_team_batting_splits(aid),
        "travel_home": ht,
        "travel_away": at,
        "raw_prob": round(raw, 4),
        "coors_penalty": coors_penalty,
        "final_home_prob": round(final_home_prob, 4),
        "run_environment": run_env,
    }


def get_all_signals(
    games: list[dict[str, Any]],
    polymarket_markets: list[dict[str, Any]],
    bankroll: float,
) -> list[dict[str, Any]]:
    """Delegate to ``polymarket_mlb_parser`` for market matching + sizing."""
    from pipeline import polymarket_mlb_parser as pmp

    return pmp.get_all_signals(games, polymarket_markets, bankroll)
