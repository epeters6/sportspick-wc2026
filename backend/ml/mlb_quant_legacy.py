import os
import sys
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Dynamically add pavlov to path so we can import the Quant models
pavlov_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "pavlov",
    "pavlov-mlb-bot",
)
if pavlov_path not in sys.path:
    sys.path.insert(0, pavlov_path)

# Bypass Pavlov's required env vars since we are just calculating probabilities
os.environ["PAVLOV_BYPASS_CONFIG"] = "1"

try:
    from pipeline.mlb_client import get_todays_games
    from pipeline.mlb_signal_engine import calculate_win_probability, schedule_row_to_game
    from backend.trading.market_matcher import _canonical
except ImportError as e:
    logger.error(f"Failed to import MLB quant engine: {e}")
    calculate_win_probability = None
    get_todays_games = None
    schedule_row_to_game = None
    _canonical = None


class MlbQuantGameIdentityAmbiguous(Exception):
    """More than one schedule row matches teams without a unique game identity."""

    CODE = "MLB_QUANT_GAME_IDENTITY_AMBIGUOUS"

    def __init__(self, message: str = "Ambiguous MLB game identity"):
        super().__init__(message)
        self.code = self.CODE


def _parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _game_start_utc(game: dict) -> datetime | None:
    """Prefer engine schedule fields; fall back to game_time_et."""
    for key in ("game_datetime", "gameDate", "game_time_et"):
        ts = _parse_utc(game.get(key))
        if ts is not None:
            return ts
    return None


def _teams_match(game: dict, home_team: str, away_team: str) -> bool:
    home_canon = (_canonical(home_team) if _canonical else None) or home_team
    away_canon = (_canonical(away_team) if _canonical else None) or away_team
    g_home_name = (game.get("home") or {}).get("name") or ""
    g_away_name = (game.get("away") or {}).get("name") or ""
    g_home = (_canonical(g_home_name) if _canonical else None) or g_home_name
    g_away = (_canonical(g_away_name) if _canonical else None) or g_away_name
    return (g_home == home_canon and g_away == away_canon) or (
        g_home_name == home_team and g_away_name == away_team
    )


def _select_schedule_game(
    games: list[dict],
    home_team: str,
    away_team: str,
    *,
    game_pk: int | str | None = None,
    scheduled_start_utc: str | datetime | None = None,
) -> dict | None:
    """
    Resolve a unique schedule row.

    Prefer exact MLB gamePk; else exact start time + teams; else unique team
    match on the requested slate. Ambiguous doubleheaders raise.
    """
    if game_pk is not None and str(game_pk).strip() != "":
        want = str(int(game_pk)) if str(game_pk).isdigit() else str(game_pk)
        by_pk = [
            g
            for g in games
            if str(g.get("game_id") or g.get("gamePk") or "") == want
        ]
        if len(by_pk) == 1:
            return by_pk[0]
        if len(by_pk) > 1:
            raise MlbQuantGameIdentityAmbiguous(
                f"Multiple schedule rows for gamePk={want}"
            )
        return None

    want_start = _parse_utc(scheduled_start_utc)
    team_matches = [g for g in games if _teams_match(g, home_team, away_team)]
    if not team_matches:
        return None

    if want_start is not None:
        timed = []
        for g in team_matches:
            g_start = _game_start_utc(g)
            if g_start is not None and g_start == want_start:
                timed.append(g)
        if len(timed) == 1:
            return timed[0]
        if len(timed) > 1:
            raise MlbQuantGameIdentityAmbiguous(
                f"Multiple schedule rows for {home_team} vs {away_team} at {want_start.isoformat()}"
            )
        # Exact time requested but no match — do not fall back to team-only
        # (would reuse the wrong game on consecutive days / doubleheaders).
        return None

    if len(team_matches) == 1:
        return team_matches[0]
    raise MlbQuantGameIdentityAmbiguous(
        f"Ambiguous doubleheader/team match for {home_team} vs {away_team} "
        f"({len(team_matches)} candidates); pass game_pk or scheduled_start_utc"
    )


def _run_quant(game: dict, home_team: str, away_team: str) -> dict | None:
    try:
        mapped = schedule_row_to_game(game)
        res = calculate_win_probability(mapped, 1000.0)  # Bankroll arbitrary for probs
        if res and res.get("final_home_prob") is not None:
            home_prob = float(res["final_home_prob"])
            return {
                "home_prob": home_prob,
                "away_prob": round(1.0 - home_prob, 4),
                "game_pk": game.get("game_id") or game.get("gamePk"),
                "slate_date": game.get("game_date") or game.get("official_date"),
            }
        logger.info(
            f"MLB quant returned no probability for {home_team} vs {away_team} "
            "(missing probables/short rest)"
        )
        return None
    except Exception as exc:
        logger.error(f"Quant model execution failed for {home_team} vs {away_team}: {exc}")
        return None


def get_mlb_quant_probability(
    home_team: str,
    away_team: str,
    *,
    slate_date: str | None = None,
    scheduled_start_utc: str | datetime | None = None,
    game_pk: int | str | None = None,
) -> dict | None:
    """
    Fetch the MLB schedule for ``slate_date`` (NY official date) and run the
    Quant Model on the exact matching game.

    Identity resolution order:
      1. ``game_pk`` / schedule ``game_id``
      2. ``scheduled_start_utc`` + home/away teams
      3. unique home/away match on that slate date

    Raises ``MlbQuantGameIdentityAmbiguous`` when multiple games match without
    a unique pk/time (e.g. doubleheaders).
    """
    if not calculate_win_probability or not get_todays_games:
        return None

    try:
        games = get_todays_games(date_str=slate_date) if slate_date else get_todays_games()
    except Exception as exc:
        logger.error(f"MLB Quant failed to fetch schedule: {exc}")
        return None

    try:
        selected = _select_schedule_game(
            games or [],
            home_team,
            away_team,
            game_pk=game_pk,
            scheduled_start_utc=scheduled_start_utc,
        )
    except MlbQuantGameIdentityAmbiguous:
        raise

    if selected is None:
        return None
    return _run_quant(selected, home_team, away_team)
