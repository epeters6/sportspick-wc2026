"""
Sync match_stats for finished fixtures and re-run settlement.
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from backend.db import get_db
from backend.sports_data.espn_football_stats import sync_football_stats_for_match
from backend.sports_data.mlb_stats_fetcher import sync_mlb_stats_for_match


def _players_empty(match_stats: dict | None) -> bool:
    players = (match_stats or {}).get("players")
    return not players


async def _backfill_player_stats(db, limit: int = 80) -> int:
    """Re-fetch ESPN stats for finished football matches missing per-player data."""
    rows = (
        db.table("matches")
        .select("id, sport, home_team, away_team, scheduled_at, external_id, is_final, match_stats")
        .eq("is_final", True)
        .not_.is_("match_stats", "null")
        .order("scheduled_at", desc=True)
        .limit(limit * 3)
        .execute()
        .data or []
    )
    candidates = [
        m for m in rows
        if (m.get("sport") or "football") != "mlb" and _players_empty(m.get("match_stats"))
    ][:limit]

    updated = 0
    for match in candidates:
        try:
            fresh = await sync_football_stats_for_match(match)
        except Exception as exc:
            logger.warning(f"Player stats backfill failed for {match.get('id')}: {exc}")
            continue
        if not fresh or not fresh.get("players"):
            continue
        merged = dict(match.get("match_stats") or {})
        merged["players"] = fresh["players"]
        if fresh.get("espn_event_id"):
            merged["espn_event_id"] = fresh["espn_event_id"]
        db.table("matches").update({
            "match_stats": merged,
            "stats_fetched_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", match["id"]).execute()
        updated += 1
    if updated:
        logger.info(f"Backfilled player stats for {updated} matches")
    return updated


async def sync_match_stats(limit: int = 80) -> int:
    """
    Fetch box score stats for finished matches missing match_stats.
    Also backfills per-player stats when team-level stats exist but players is empty.
    Returns count updated.
    """
    db = get_db()
    rows = (
        db.table("matches")
        .select("id, sport, home_team, away_team, scheduled_at, external_id, is_final, match_stats")
        .eq("is_final", True)
        .is_("match_stats", "null")
        .order("scheduled_at", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )
    updated = 0
    for match in rows:
        stats = None
        sport = match.get("sport") or "football"
        try:
            if sport == "mlb":
                stats = await sync_mlb_stats_for_match(match)
            else:
                stats = await sync_football_stats_for_match(match)
        except Exception as exc:
            logger.warning(f"Stats fetch failed for {match.get('id')}: {exc}")
            continue
        if not stats:
            continue
        # Merge HT from openfootball if ESPN missing half
        if not stats.get("half", {}).get("home") and match.get("match_stats"):
            old_half = (match.get("match_stats") or {}).get("half")
            if old_half:
                stats["half"] = old_half
        db.table("matches").update({
            "match_stats": stats,
            "stats_fetched_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", match["id"]).execute()
        updated += 1
    player_backfill = await _backfill_player_stats(db, limit=limit)
    if updated:
        logger.info(f"Synced stats for {updated} matches")
    return updated + player_backfill


async def enrich_openfootball_ht() -> int:
    """Store half-time scores from openfootball into match_stats when available."""
    import httpx

    db = get_db()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
            )
            r.raise_for_status()
            of_matches = r.json().get("matches") or []
    except Exception as exc:
        logger.warning(f"openfootball HT fetch failed: {exc}")
        return 0

    updated = 0
    for raw in of_matches:
        ht = (raw.get("score") or {}).get("ht")
        if not ht or len(ht) != 2:
            continue
        t1, t2 = raw.get("team1"), raw.get("team2")
        db_rows = (
            db.table("matches")
            .select("id, match_stats")
            .eq("home_team", t1)
            .eq("away_team", t2)
            .eq("is_final", True)
            .limit(1)
            .execute()
            .data or []
        )
        if not db_rows:
            continue
        row = db_rows[0]
        stats = dict(row.get("match_stats") or {})
        stats.setdefault("source", "openfootball")
        stats["half"] = {"home": ht[0], "away": ht[1]}
        db.table("matches").update({"match_stats": stats}).eq("id", row["id"]).execute()
        updated += 1
    return updated
