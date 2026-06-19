"""
World Cup 2026 data fetcher.

Primary source:  wc2026api.com (free tier — requires API key)
Fallback source: openfootball/worldcup GitHub JSON (no key needed)
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.config import get_settings
from backend.db import get_db

OPENFOOTBALL_BASE = (
    "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026"
)

settings = get_settings()


# ─── Primary: wc2026api.com ──────────────────────────────────────────────────

class WorldCupApiFetcher:
    BASE = settings.wc_api_base

    def __init__(self):
        self.headers = {"x-api-key": settings.wc_api_key}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def get_matches(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self.BASE}/matches", headers=self.headers)
            r.raise_for_status()
            return r.json().get("matches", [])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def get_live_scores(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self.BASE}/matches/live", headers=self.headers)
            r.raise_for_status()
            return r.json().get("matches", [])


# ─── Fallback: openfootball GitHub JSON ─────────────────────────────────────

class OpenfootballFetcher:
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def get_matches(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{OPENFOOTBALL_BASE}/worldcup.json")
            r.raise_for_status()
            data = r.json()
            # Flat matches array (2026 format)
            return data.get("matches", [])


# ─── Normaliser ─────────────────────────────────────────────────────────────

def _normalise_primary(raw: dict) -> dict:
    """Map wc2026api.com fields → our DB schema."""
    score = raw.get("score", {})
    home = score.get("home")
    away = score.get("away")
    winner = None
    if home is not None and away is not None:
        if home > away:
            winner = raw.get("homeTeam", {}).get("name")
        elif away > home:
            winner = raw.get("awayTeam", {}).get("name")
        else:
            winner = "draw"
    return {
        "external_id": str(raw.get("id", "")),
        "tournament": "FIFA World Cup 2026",
        "sport": "football",
        "home_team": raw.get("homeTeam", {}).get("name", ""),
        "away_team": raw.get("awayTeam", {}).get("name", ""),
        "scheduled_at": raw.get("utcDate"),
        "home_score": home,
        "away_score": away,
        "winner": winner,
        "stage": raw.get("stage", ""),
        "venue": raw.get("venue", {}).get("name", ""),
        "is_final": raw.get("status") == "FINISHED",
        "finished_at": raw.get("utcDate") if raw.get("status") == "FINISHED" else None,
    }


def _normalise_fallback(raw: dict) -> dict:
    """Map openfootball 2026 flat format → our DB schema."""
    score_ft = raw.get("score", {}).get("ft")  # [home, away] or None
    score1 = score_ft[0] if score_ft else None
    score2 = score_ft[1] if score_ft else None
    team1 = raw.get("team1", "")
    team2 = raw.get("team2", "")
    winner = None
    if score1 is not None and score2 is not None:
        if score1 > score2:
            winner = team1
        elif score2 > score1:
            winner = team2
        else:
            winner = "draw"
    date_str = raw.get("date", "")
    # Stable external ID from date + teams
    external_id = f"of_{date_str}_{team1}_{team2}".replace(" ", "_")
    stage = raw.get("group") or raw.get("round", "")
    return {
        "external_id": external_id,
        "tournament": "FIFA World Cup 2026",
        "sport": "football",
        "home_team": team1,
        "away_team": team2,
        "scheduled_at": date_str or None,
        "home_score": score1,
        "away_score": score2,
        "winner": winner,
        "stage": stage,
        "venue": raw.get("ground", ""),
        "is_final": score1 is not None,
        "finished_at": date_str if score1 is not None else None,
    }


# ─── Sync to DB ─────────────────────────────────────────────────────────────

async def sync_matches() -> int:
    """Fetch matches from best available source and upsert into Supabase."""
    db = get_db()
    raw_matches: list[dict] = []
    normalise = _normalise_primary

    if settings.wc_api_key:
        try:
            fetcher = WorldCupApiFetcher()
            raw_matches = await fetcher.get_matches()
            logger.info(f"Fetched {len(raw_matches)} matches from wc2026api.com")
        except Exception as exc:
            logger.warning(f"Primary WC API failed ({exc}), falling back to openfootball")

    if not raw_matches:
        try:
            fallback = OpenfootballFetcher()
            raw_matches = await fallback.get_matches()
            normalise = _normalise_fallback
            logger.info(f"Fetched {len(raw_matches)} matches from openfootball")
        except Exception as exc:
            logger.error(f"Both WC data sources failed: {exc}")
            return 0

    records = [normalise(m) for m in raw_matches if m]
    if not records:
        return 0

    # Upsert — conflict on external_id
    result = db.table("matches").upsert(records, on_conflict="external_id").execute()
    count = len(result.data or [])
    logger.info(f"Upserted {count} match records")
    return count


async def link_picks_to_matches() -> int:
    """
    Assign match_id to picks that have a predicted_winner but no linked match.

    Heuristic: find matches featuring the predicted team, then choose the one
    whose scheduled_at is closest to (and ideally after) the pick's posted_at.
    Falls back to the soonest upcoming match featuring that team.
    """
    db = get_db()

    unlinked = (
        db.table("picks")
        .select("id, predicted_winner, posted_at")
        .is_("match_id", "null")
        .not_.is_("predicted_winner", "null")
        .execute()
        .data or []
    )
    if not unlinked:
        return 0

    matches = (
        db.table("matches")
        .select("id, home_team, away_team, scheduled_at")
        .execute()
        .data or []
    )
    if not matches:
        return 0

    # Build alias → canonical match name mapping from TEAM_ALIASES
    from backend.scrapers.pick_extractor import TEAM_ALIASES
    # Reverse map: canonical → list of aliases (including itself)
    alias_to_canonical: dict[str, str] = {}
    for alias, canonical in TEAM_ALIASES.items():
        alias_to_canonical[alias] = canonical
    # Also map match team names to themselves
    for m in matches:
        for team in (m.get("home_team"), m.get("away_team")):
            if team:
                alias_to_canonical[team.lower()] = team

    # Index matches by team name (and all known aliases)
    by_team: dict[str, list[dict]] = {}
    for m in matches:
        for team in (m.get("home_team"), m.get("away_team")):
            if not team:
                continue
            by_team.setdefault(team, []).append(m)
            # Also index by canonical from aliases
            for alias, canonical in TEAM_ALIASES.items():
                if canonical == team:
                    by_team.setdefault(alias.title(), []).append(m)

    def _parse(dt: str | None) -> datetime | None:
        if not dt:
            return None
        try:
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None

    linked = 0
    for pick in unlinked:
        team = pick["predicted_winner"]
        candidates = by_team.get(team)
        # Alias fallback: try lowercase alias lookup
        if not candidates and team:
            canonical = alias_to_canonical.get(team.lower())
            if canonical:
                candidates = by_team.get(canonical)
        if not candidates:
            continue

        posted = _parse(pick.get("posted_at"))

        def _sort_key(m: dict):
            sched = _parse(m.get("scheduled_at"))
            if sched is None:
                return (2, 0)
            if posted is not None:
                # Prefer matches at/after the post, soonest first
                delta = (sched - posted).total_seconds()
                if delta >= 0:
                    return (0, delta)
                return (1, -delta)
            return (0, abs(sched.timestamp()))

        best = sorted(candidates, key=_sort_key)[0]
        try:
            db.table("picks").update({"match_id": best["id"]}).eq("id", pick["id"]).execute()
            linked += 1
        except Exception as exc:
            msg = str(exc)
            if "picks_influencer_match_unique" in msg:
                # Another pick from the same influencer is already linked to this
                # match — this is a duplicate video pick; delete the orphan.
                try:
                    db.table("picks").delete().eq("id", pick["id"]).execute()
                except Exception:
                    pass
            else:
                logger.warning(f"Failed to link pick {pick['id']}: {exc}")

    logger.info(f"Linked {linked} picks to matches")
    return linked


async def resolve_pending_picks() -> int:
    """
    For every pick with outcome='pending' whose match is now finished,
    compare predicted_winner to match.winner and set correct/incorrect.
    """
    db = get_db()
    finished = (
        db.table("matches")
        .select("id, winner")
        .eq("is_final", True)
        .execute()
        .data or []
    )
    if not finished:
        return 0

    resolved = 0
    for match in finished:
        mid = match["id"]
        actual_winner = match["winner"]
        pending_picks = (
            db.table("picks")
            .select("id, predicted_winner")
            .eq("match_id", mid)
            .eq("outcome", "pending")
            .execute()
            .data or []
        )
        for pick in pending_picks:
            outcome = (
                "correct"
                if pick["predicted_winner"] == actual_winner
                else "incorrect"
            )
            db.table("picks").update(
                {"outcome": outcome, "resolved_at": datetime.utcnow().isoformat()}
            ).eq("id", pick["id"]).execute()
            resolved += 1

    logger.info(f"Resolved {resolved} pending picks")
    return resolved


if __name__ == "__main__":
    asyncio.run(sync_matches())
