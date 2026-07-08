"""Starting-pitcher scoring from MLB stats + learned multipliers in ``logs/pitcher_scores.json``."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any

import data_paths as dp
from pipeline import mlb_client

logger = logging.getLogger(__name__)

_SCORES_PATH = os.path.join(dp.logs_dir(), "pitcher_scores.json")
_pitcher_scores: dict[str, float] = {}


def _load_pitcher_scores() -> None:
    global _pitcher_scores
    try:
        with open(_SCORES_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            _pitcher_scores = {str(k): float(v) for k, v in raw.items()}
        else:
            _pitcher_scores = {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        _pitcher_scores = {}


def _save_pitcher_scores() -> None:
    os.makedirs(os.path.dirname(_SCORES_PATH), exist_ok=True)
    with open(_SCORES_PATH, "w", encoding="utf-8") as fh:
        json.dump(_pitcher_scores, fh, indent=2, sort_keys=True)


_load_pitcher_scores()


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _parse_game_date(game_date: str | date) -> date:
    if isinstance(game_date, date):
        return game_date
    return datetime.strptime(str(game_date)[:10], "%Y-%m-%d").date()


def _normalize_throws(throws: str | None) -> str:
    if not throws:
        return "R"
    c = str(throws).strip().upper()[:1]
    return "L" if c == "L" else "R"


def _days_rest_before_game(pitcher_id: int, game_date: date) -> int | None:
    """Calendar gap minus one between *game_date* and the pitcher’s prior start (MLB-style off days)."""
    season = game_date.year
    try:
        j = mlb_client.get_json(
            f"/people/{int(pitcher_id)}/stats",
            params={"stats": "gameLog", "group": "pitching", "season": season},
        )
    except Exception as exc:
        logger.debug("pitcher_analyzer: gameLog for rest days failed — %s", exc)
        return None
    splits: list[dict] = []
    for block in j.get("stats") or []:
        if (block.get("type") or {}).get("displayName") == "gameLog":
            splits = list(block.get("splits") or [])
            break
    starts: list[dict] = [
        sp
        for sp in splits
        if int((sp.get("stat") or {}).get("gamesStarted") or 0) >= 1
    ]
    starts.sort(key=lambda sp: str(sp.get("date") or ""), reverse=True)
    for sp in starts:
        ds = sp.get("date")
        if not ds:
            continue
        try:
            last = datetime.strptime(str(ds)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if last >= game_date:
            continue
        return max(0, (game_date - last).days - 1)
    return None


def _rest_score(days_rest: int | None) -> tuple[int, str | None]:
    if days_rest is None:
        return 0, None
    if days_rest >= 5:
        return 6, str(days_rest)
    if days_rest == 4:
        return 0, str(days_rest)
    if days_rest == 3:
        return -8, str(days_rest)
    if days_rest <= 2:
        return -18, str(days_rest)
    return 0, str(days_rest)


def _vs_team_adjustment(season_era: float | None, vs_era: float | None, vs_games: int) -> tuple[float, str]:
    if season_era is None or vs_era is None or vs_games <= 0:
        return 0.0, "neutral (no split)"
    if vs_era > season_era * 1.20:
        return -10.0, "struggles vs opponent (+ERA)"
    if vs_era < season_era * 0.85:
        return 8.0, "handles opponent well"
    return 0.0, "neutral"


def _matchup_score(throws: str, splits: dict) -> tuple[float, str]:
    """Handedness vs opponent AVG vs LHP/RHP (statSplits sitCodes)."""
    if throws == "R":
        avg = splits.get("vs_rhp_avg")
        tag = "vs RHP"
        hi = 0.270
        lo = 0.225
    else:
        avg = splits.get("vs_lhp_avg")
        tag = "vs LHP"
        hi = 0.270
        lo = 0.225
    if avg is None:
        return 0.0, f"no {tag} data"
    if avg < lo:
        return 10.0, f"opp {tag} weak ({avg:.3f})"
    if avg > hi:
        return -10.0, f"opp {tag} strong ({avg:.3f})"
    return 0.0, f"opp {tag} neutral ({avg:.3f})"


def _advantage_label(final_score: float) -> str:
    if final_score >= 72:
        return "strong"
    if final_score >= 58:
        return "favorable"
    if final_score >= 45:
        return "neutral"
    if final_score >= 32:
        return "below_avg"
    return "poor"


def analyze_pitcher(
    pitcher_id: int,
    opposing_team_id: int,
    throws: str,
    game_date: str | date,
) -> dict[str, Any]:
    """
    Composite pitcher rating (0–100-ish before multiplier) using season line, recent games,
    opponent history, platoon matchup, and rest.
    """
    gid = int(pitcher_id)
    oid = int(opposing_team_id)
    th = _normalize_throws(throws)
    gdt = _parse_game_date(game_date)

    season_stats = mlb_client.get_pitcher_stats(gid, last_n_days=30)
    vs_team = mlb_client.get_pitcher_vs_team(gid, oid)
    opp_batting_splits = mlb_client.get_team_batting_splits(oid)

    era = season_stats.get("era")
    era_score = max(0.0, 100.0 - (float(era) * 12.0)) if era is not None else 50.0

    scores = list(season_stats.get("last_5_game_scores") or [])[:3]
    if scores:
        recent_score = float(sum(scores) / len(scores))
        recent_score = _clamp(recent_score, 0.0, 100.0)
    else:
        recent_score = 50.0

    vs_adj, vs_note = _vs_team_adjustment(era, vs_team.get("era"), int(vs_team.get("games") or 0))
    matchup_pts, matchup_note = _matchup_score(th, opp_batting_splits)

    days_rest = _days_rest_before_game(gid, gdt)
    rest_pts, rest_detail = _rest_score(days_rest)

    pitcher_score = (
        era_score * 0.4
        + recent_score * 0.35
        + vs_adj
        + matchup_pts
        + rest_pts
    )

    learned = _pitcher_scores.get(str(gid), 1.0)
    final_score = pitcher_score * learned
    final_score = _clamp(final_score, 0.0, 150.0)

    return {
        "pitcher_id": gid,
        "opposing_team_id": oid,
        "throws": th,
        "game_date": gdt.isoformat(),
        "season_stats": season_stats,
        "vs_team": vs_team,
        "opp_batting_splits": opp_batting_splits,
        "last_3_game_scores_used": scores,
        "era": era,
        "era_score": round(era_score, 2),
        "recent_score": round(recent_score, 2),
        "vs_team_adjustment": vs_adj,
        "vs_team_note": vs_note,
        "matchup_score": matchup_pts,
        "matchup_note": matchup_note,
        "days_rest": days_rest,
        "rest_detail": rest_detail,
        "rest_score": rest_pts,
        "learned_multiplier": learned,
        "pitcher_score": round(pitcher_score, 2),
        "final_pitcher_score": round(final_score, 2),
        "advantage_label": _advantage_label(final_score),
    }


def calculate_pitcher_matchup_prob(home_analysis: dict, away_analysis: dict) -> float:
    """Home-win probability tilt from starter gap + baseline home edge."""
    h = float(home_analysis.get("final_pitcher_score", home_analysis.get("pitcher_score", 50)))
    a = float(away_analysis.get("final_pitcher_score", away_analysis.get("pitcher_score", 50)))
    score_diff = h - a
    base = 0.54
    prob = base + (score_diff * 0.0035)
    return _clamp(prob, 0.30, 0.75)


def update_pitcher_score(pitcher_id: int, model_was_correct: bool) -> float:
    """Bayesian-style nudge on the stored multiplier after an outcome."""
    key = str(int(pitcher_id))
    score = float(_pitcher_scores.get(key, 1.0))
    if model_was_correct:
        score *= 1.05
    else:
        score *= 0.92
    score = _clamp(score, 0.50, 1.50)
    _pitcher_scores[key] = score
    _save_pitcher_scores()
    return score
