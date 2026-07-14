"""
MLB data fetcher using the free MLB Stats API (statsapi.mlb.com).

No API key required. Fetches today's and tomorrow's schedule, upserts
into the matches table with sport='mlb', and resolves pending MLB picks
the same way worldcup_fetcher does for soccer.

Endpoint docs: https://statsapi.mlb.com/docs/
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.db import get_db

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_TOURNAMENT = "MLB 2026"
SPORT_ID = 1  # MLB
DAYS_AHEAD = 3   # fetch schedule this many days ahead
DAYS_BACK = 7    # re-fetch recent games so is_final gets set after game day


# ─── Team name normalisation ─────────────────────────────────────────────────

MLB_TEAM_ALIASES: dict[str, str] = {
    # Use full franchise names as canonical (matching statsapi)
    "yankees": "New York Yankees",
    "new york yankees": "New York Yankees",
    "red sox": "Boston Red Sox",
    "boston red sox": "Boston Red Sox",
    "dodgers": "Los Angeles Dodgers",
    "la dodgers": "Los Angeles Dodgers",
    "los angeles dodgers": "Los Angeles Dodgers",
    "mets": "New York Mets",
    "new york mets": "New York Mets",
    "cubs": "Chicago Cubs",
    "chicago cubs": "Chicago Cubs",
    "astros": "Houston Astros",
    "houston astros": "Houston Astros",
    "braves": "Atlanta Braves",
    "atlanta braves": "Atlanta Braves",
    "phillies": "Philadelphia Phillies",
    "philadelphia phillies": "Philadelphia Phillies",
    "giants": "San Francisco Giants",
    "san francisco giants": "San Francisco Giants",
    "cardinals": "St. Louis Cardinals",
    "st louis cardinals": "St. Louis Cardinals",
    "padres": "San Diego Padres",
    "san diego padres": "San Diego Padres",
    "blue jays": "Toronto Blue Jays",
    "toronto blue jays": "Toronto Blue Jays",
    "rangers": "Texas Rangers",
    "texas rangers": "Texas Rangers",
    "mariners": "Seattle Mariners",
    "seattle mariners": "Seattle Mariners",
    "twins": "Minnesota Twins",
    "minnesota twins": "Minnesota Twins",
    "rays": "Tampa Bay Rays",
    "tampa bay rays": "Tampa Bay Rays",
    "orioles": "Baltimore Orioles",
    "baltimore orioles": "Baltimore Orioles",
    "angels": "Los Angeles Angels",
    "la angels": "Los Angeles Angels",
    "los angeles angels": "Los Angeles Angels",
    "white sox": "Chicago White Sox",
    "chicago white sox": "Chicago White Sox",
    "tigers": "Detroit Tigers",
    "detroit tigers": "Detroit Tigers",
    "guardians": "Cleveland Guardians",
    "cleveland guardians": "Cleveland Guardians",
    "royals": "Kansas City Royals",
    "kansas city royals": "Kansas City Royals",
    "athletics": "Oakland Athletics",
    "oakland athletics": "Oakland Athletics",
    "a's": "Oakland Athletics",
    "marlins": "Miami Marlins",
    "miami marlins": "Miami Marlins",
    "nationals": "Washington Nationals",
    "washington nationals": "Washington Nationals",
    "pirates": "Pittsburgh Pirates",
    "pittsburgh pirates": "Pittsburgh Pirates",
    "reds": "Cincinnati Reds",
    "cincinnati reds": "Cincinnati Reds",
    "brewers": "Milwaukee Brewers",
    "milwaukee brewers": "Milwaukee Brewers",
    "rockies": "Colorado Rockies",
    "colorado rockies": "Colorado Rockies",
    "diamondbacks": "Arizona Diamondbacks",
    "d-backs": "Arizona Diamondbacks",
    "arizona diamondbacks": "Arizona Diamondbacks",
}


def canonicalise_mlb_team(raw: str) -> str | None:
    """Return canonical MLB team name from alias or None if unknown."""
    raw = raw.strip().lower()
    if raw in MLB_TEAM_ALIASES:
        return MLB_TEAM_ALIASES[raw]
    import re
    for alias, canonical in MLB_TEAM_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", raw):
            return canonical
    return None


# ─── Fetcher ─────────────────────────────────────────────────────────────────

class MLBFetcher:
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def get_schedule(self, date_str: str) -> list[dict]:
        """Fetch MLB schedule for a single date (YYYY-MM-DD)."""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                MLB_SCHEDULE_URL,
                params={
                    "sportId": SPORT_ID,
                    "date": date_str,
                    "hydrate": "linescore,decisions,team,probablePitcher",
                },
            )
            r.raise_for_status()
            dates = r.json().get("dates", [])
            games = []
            for d in dates:
                games.extend(d.get("games", []))
            return games


def _normalise_mlb_game(raw: dict) -> dict:
    """Map statsapi game → our DB schema."""
    teams = raw.get("teams", {})
    home = teams.get("home", {}).get("team", {})
    away = teams.get("away", {}).get("team", {})
    home_name = home.get("name", "")
    away_name = away.get("name", "")

    linescore = raw.get("linescore", {})
    home_score = linescore.get("teams", {}).get("home", {}).get("runs")
    away_score = linescore.get("teams", {}).get("away", {}).get("runs")

    status = raw.get("status", {}).get("abstractGameState", "")  # "Live", "Final", "Preview"
    is_final = status == "Final"

    winner = None
    if is_final and home_score is not None and away_score is not None:
        if home_score > away_score:
            winner = home_name
        elif away_score > home_score:
            winner = away_name
        # MLB can't draw — no "draw" case needed

    game_date = raw.get("gameDate", "")  # ISO 8601
    external_id = f"mlb_{raw.get('gamePk', '')}"

    probable: dict[str, dict] = {}
    for side, team_name in (("home", home_name), ("away", away_name)):
        pp = (teams.get(side, {}) or {}).get("probablePitcher") or {}
        if pp.get("fullName"):
            probable[side] = {
                "id": pp.get("id"),
                "name": pp.get("fullName"),
                "team": team_name,
            }

    record: dict = {
        "external_id": external_id,
        "tournament": MLB_TOURNAMENT,
        "sport": "mlb",
        "home_team": home_name,
        "away_team": away_name,
        "scheduled_at": game_date or None,
        "home_score": home_score,
        "away_score": away_score,
        "winner": winner,
        "stage": raw.get("seriesDescription", "Regular Season"),
        "venue": raw.get("venue", {}).get("name", ""),
        "is_final": is_final,
        "finished_at": game_date if is_final else None,
    }
    if probable and not is_final:
        record["match_stats"] = {"probable_pitchers": probable, "source": "mlb_schedule"}
    return record


# ─── Sync to DB ─────────────────────────────────────────────────────────────

async def sync_mlb_matches() -> int:
    """Fetch MLB schedule for today + next DAYS_AHEAD days and upsert to DB."""
    db = get_db()
    fetcher = MLBFetcher()
    all_records: list[dict] = []

    today = datetime.now(timezone.utc).date()
    dates = [
        today + timedelta(days=i)
        for i in range(-DAYS_BACK, DAYS_AHEAD + 1)
    ]

    for d in dates:
        date_str = d.isoformat()
        try:
            games = await fetcher.get_schedule(date_str)
            records = [_normalise_mlb_game(g) for g in games]
            all_records.extend(records)
        except Exception as exc:
            logger.warning(f"MLB schedule fetch failed for {date_str}: {exc}")

    if not all_records:
        return 0

    # Dedupe by external_id (doubleheaders / overlapping date windows)
    by_id: dict[str, dict] = {}
    for rec in all_records:
        eid = rec.get("external_id")
        if eid:
            by_id[eid] = rec
    all_records = list(by_id.values())

    result = db.table("matches").upsert(all_records, on_conflict="external_id").execute()
    count = len(result.data or [])
    logger.info(f"MLB: upserted {count} game records")

    # Belt-and-suspenders: anything still open whose start was >12h ago is done.
    # ESPN status misses happen; without this autobet keeps hunting dead markets.
    from datetime import timezone as _tz
    cutoff = (datetime.now(_tz.utc) - timedelta(hours=12)).isoformat()
    try:
        stale = (
            db.table("matches")
            .update({"is_final": True})
            .eq("sport", "mlb")
            .eq("is_final", False)
            .lt("scheduled_at", cutoff)
            .execute()
        )
        n_stale = len(stale.data or [])
        if n_stale:
            logger.info(f"MLB: finalized {n_stale} stale open matches")
    except Exception as exc:
        logger.warning(f"MLB stale-finalization failed: {exc}")

    return count


async def link_mlb_picks_to_matches() -> int:
    """
    Assign match_id to MLB picks missing a linked game.

    Uses shared text/team heuristics plus MLB Stats API player→team lookup
    for player props that don't name a fixture explicitly.
    """
    db = get_db()

    unlinked = (
        db.table("picks")
        .select(
            "id, predicted_winner, posted_at, bet_type, bet_line, bet_subject, raw_text"
        )
        .is_("match_id", "null")
        .not_.is_("predicted_winner", "null")
        .execute()
        .data or []
    )
    if not unlinked:
        return 0

    mlb_matches = (
        db.table("matches")
        .select(
            "id, home_team, away_team, scheduled_at, is_final, match_stats, sport"
        )
        .eq("sport", "mlb")
        .execute()
        .data or []
    )
    if not mlb_matches:
        return 0

    from backend.sports_data.pick_linking import (
        build_match_index,
        infer_match_candidates,
        pick_best_match,
    )
    from backend.sports_data.mlb_player_linking import infer_mlb_match_for_player_pick

    by_team, alias_to_canonical = build_match_index(mlb_matches)

    linked = 0
    for pick in unlinked:
        candidates = infer_match_candidates(pick, mlb_matches, by_team, alias_to_canonical)
        best = pick_best_match(candidates, pick.get("posted_at"), pick.get("raw_text"))
        if not best:
            best = infer_mlb_match_for_player_pick(pick, mlb_matches)
        if not best:
            continue
        try:
            db.table("picks").update({"match_id": best["id"]}).eq("id", pick["id"]).execute()
            linked += 1
        except Exception as exc:
            msg = str(exc)
            if "picks_influencer_match_unique" in msg or "duplicate key" in msg.lower():
                logger.debug(f"MLB pick {pick['id']} already linked (duplicate influencer/match)")
            else:
                logger.warning(f"Failed to link MLB pick {pick['id']}: {exc}")

    logger.info(f"MLB: linked {linked} picks to games")
    return linked


async def resolve_mlb_picks() -> int:
    """MLB picks use the shared resolver (supports props + moneyline)."""
    from backend.sports_data.pick_resolver import resolve_all_pending_picks
    return resolve_all_pending_picks()


if __name__ == "__main__":
    asyncio.run(sync_mlb_matches())
