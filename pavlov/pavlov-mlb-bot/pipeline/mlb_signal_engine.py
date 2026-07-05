"""
MLB win-probability stack → Polymarket edges.

Model design (additive edges, centered at 50/50):

    final_home_prob = clamp(0.50 + home_field + sum(component_edges), 0.10, 0.90)

Each ``component_edge`` is a signed probability adjustment (in points) relative to
a 50/50 coin flip. This avoids the prior bug where every component was anchored
at 0.54 with tight clamps, mathematically pinning ``final_home_prob`` near 0.50
even on lopsided matchups.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from pipeline import mlb_client, park_factors, pitcher_analyzer, travel_calculator
from pipeline.bullpen_tracker import analyze_bullpen

logger = logging.getLogger(__name__)

# Empirical MLB home-team win rate is ~54%, so the additive home-field edge is
# +0.04 in probability points relative to a coin flip.
HOME_FIELD_EDGE = 0.040

# Pitcher score gap: each 1-point gap shifts win prob by 0.0050 (max ±0.20 even
# at extreme matchups). Pitching is the dominant signal in baseball.
PITCHER_PT_PER_SCORE_POINT = 0.0050
PITCHER_EDGE_MAX = 0.22

# Bullpen: 60-point gap (e.g. fresh vs exhausted) → ±0.12 swing.
BULLPEN_PT_PER_STRENGTH_POINT = 0.0020
BULLPEN_EDGE_MAX = 0.12

# Form: 1 run/game gap → ±0.025 swing.
FORM_PT_PER_RUN = 0.025
FORM_EDGE_MAX = 0.06

# Lineup matchup vs hand: ±0.05 max.
LINEUP_EDGE_MAX = 0.05

# Travel: turn away fatigue penalty into home edge directly (range up to ~0.025).
TRAVEL_EDGE_MAX = 0.04


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

    # Pitcher edge — driven by the gap in composite pitcher scores.
    h_pscore = float(hp.get("final_pitcher_score") or hp.get("pitcher_score") or 50.0)
    a_pscore = float(ap.get("final_pitcher_score") or ap.get("pitcher_score") or 50.0)
    pitcher_edge = _clamp(
        (h_pscore - a_pscore) * PITCHER_PT_PER_SCORE_POINT,
        -PITCHER_EDGE_MAX,
        PITCHER_EDGE_MAX,
    )
    # Legacy view: pitcher_prob anchored at 0.50 + edge (used by confidence calc).
    pitcher_prob = _clamp(0.50 + pitcher_edge, 0.20, 0.80)

    # Bullpen edge — strength is 100 - fatigue (fresh→100, exhausted→0-ish).
    hb = analyze_bullpen(hid)
    ab = analyze_bullpen(aid)
    bullpen_edge = _clamp(
        (float(hb["strength"]) - float(ab["strength"])) * BULLPEN_PT_PER_STRENGTH_POINT,
        -BULLPEN_EDGE_MAX,
        BULLPEN_EDGE_MAX,
    )
    bullpen_prob = _clamp(0.50 + bullpen_edge, 0.30, 0.70)

    # Recent form — runs scored AND runs allowed differential.
    hf = mlb_client.get_team_last_n_games(hid, 10)
    af = mlb_client.get_team_last_n_games(aid, 10)
    h_rs = float(hf.get("avg_runs_scored") or 4.5)
    a_rs = float(af.get("avg_runs_scored") or 4.5)
    h_ra = float(hf.get("avg_runs_allowed") or 4.5)
    a_ra = float(af.get("avg_runs_allowed") or 4.5)
    # Run differential matters more than offense alone.
    h_diff = h_rs - h_ra
    a_diff = a_rs - a_ra
    form_edge = _clamp((h_diff - a_diff) * FORM_PT_PER_RUN, -FORM_EDGE_MAX, FORM_EDGE_MAX)
    form_prob = _clamp(0.50 + form_edge, 0.30, 0.70)

    # Lineup matchup vs opposing starter handedness (symmetric, ±0.05).
    lineup_edge = 0.0
    hp_opp = _opposing_split_avg(hp)  # away team avg vs home pitcher hand
    ap_opp = _opposing_split_avg(ap)  # home team avg vs away pitcher hand
    if hp_opp is not None:
        if hp_opp < 0.220:
            lineup_edge += 0.04  # away can't hit home starter
        elif hp_opp < 0.235:
            lineup_edge += 0.02
        elif hp_opp > 0.280:
            lineup_edge -= 0.04
        elif hp_opp > 0.265:
            lineup_edge -= 0.02
    if ap_opp is not None:
        if ap_opp < 0.220:
            lineup_edge -= 0.04  # home can't hit away starter
        elif ap_opp < 0.235:
            lineup_edge -= 0.02
        elif ap_opp > 0.280:
            lineup_edge += 0.04
        elif ap_opp > 0.265:
            lineup_edge += 0.02
    lineup_edge = _clamp(lineup_edge, -LINEUP_EDGE_MAX, LINEUP_EDGE_MAX)
    lineup_prob = _clamp(0.50 + lineup_edge, 0.40, 0.60)

    run_env = park_factors.get_run_environment(venue, gdate, is_dome)
    total_run_factor = float(run_env.get("total_factor", 1.0))
    
    # Scale pitcher and bullpen edge inversely to the run environment.
    # In a hitter-friendly park with wind blowing out (total_factor > 1.0),
    # pitching predictability drops. In a pitcher's park (total_factor < 1.0), pitching dominates.
    pitcher_edge = pitcher_edge / total_run_factor
    bullpen_edge = bullpen_edge / total_run_factor

    # Travel: home is always 0 fatigue (function returns zeros). Away fatigue
    # penalty → positive home edge. (Previous code subtracted away fatigue,
    # which inverted the sign.)
    ht = travel_calculator.calculate_fatigue(hid, gdate, True)
    at = travel_calculator.calculate_fatigue(aid, gdate, False)
    travel_edge = _clamp(
        float(at["fatigue_penalty"]) - float(ht["fatigue_penalty"]),
        -TRAVEL_EDGE_MAX,
        TRAVEL_EDGE_MAX,
    )
    travel_prob = _clamp(0.50 + travel_edge, 0.40, 0.60)

    # Coors penalty: extreme run env reduces home pitching edge predictability,
    # so trim some home prob.
    import math
    
    def to_logit(prob):
        p = _clamp(prob, 0.05, 0.95)
        return math.log(p / (1 - p))
        
    def to_prob(logit):
        return 1 / (1 + math.exp(-logit))

    # Convert all component probabilities to logits (relative to 0)
    # The probabilities were anchored at 0.50, so we can convert directly.
    sum_logits = (
        to_logit(0.50 + HOME_FIELD_EDGE)
        + to_logit(pitcher_prob)
        + to_logit(bullpen_prob)
        + to_logit(form_prob)
        + to_logit(lineup_prob)
        + to_logit(travel_prob)
    )
    
    # We subtract the baseline logits (since we added 0.50 6 times)
    # Actually, to_logit(0.50) = 0, so it doesn't matter!
    
    # Coors penalty is tricky in logit space. We'll apply it directly to probability later.
    raw = to_prob(sum_logits)
    coors_penalty = 0.03 if "Coors" in venue else 0.0
    
    final_home_prob = _clamp(raw - coors_penalty, 0.10, 0.90)
    raw_edge = raw - 0.50

    return {
        "pitcher_prob": round(pitcher_prob, 4),
        "bullpen_prob": round(bullpen_prob, 4),
        "form_prob": round(form_prob, 4),
        "lineup_prob": round(lineup_prob, 4),
        "park_baseline_prob": 0.50,
        "travel_prob": round(travel_prob, 4),
        "pitcher_edge": round(pitcher_edge, 4),
        "bullpen_edge": round(bullpen_edge, 4),
        "form_edge": round(form_edge, 4),
        "lineup_edge": round(lineup_edge, 4),
        "travel_edge": round(travel_edge, 4),
        "home_field_edge": HOME_FIELD_EDGE,
        "edge_caps": {
            "pitcher": PITCHER_EDGE_MAX,
            "bullpen": BULLPEN_EDGE_MAX,
            "form": FORM_EDGE_MAX,
            "lineup": LINEUP_EDGE_MAX,
            "travel": TRAVEL_EDGE_MAX,
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
        "raw_edge": round(raw_edge, 4),
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
