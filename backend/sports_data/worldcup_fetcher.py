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

def _winner_from_scores(
    team1: str,
    team2: str,
    ft: list | None,
    pen: list | None = None,
) -> tuple[int | None, int | None, str | None]:
    """
    Derive (home_score, away_score, winner) from full-time and optional penalty scores.

    Knockout ties are decided on penalties — Polymarket "Will X win?" resolves on
    the advancing team, not a 90-min draw.
    """
    if not ft or len(ft) < 2:
        return None, None, None
    score1, score2 = ft[0], ft[1]
    if score1 > score2:
        return score1, score2, team1
    if score2 > score1:
        return score1, score2, team2
    # FT draw — check penalty shootout
    if pen and len(pen) >= 2:
        p1, p2 = pen[0], pen[1]
        if p1 > p2:
            return score1, score2, team1
        if p2 > p1:
            return score1, score2, team2
    return score1, score2, "draw"


def _normalise_primary(raw: dict) -> dict:
    """Map wc2026api.com fields → our DB schema."""
    score = raw.get("score", {}) or {}
    home = score.get("home")
    away = score.get("away")
    ft = [home, away] if home is not None and away is not None else None
    pen = score.get("penalties") or score.get("penalty") or score.get("p")
    if isinstance(pen, dict):
        pen = [pen.get("home"), pen.get("away")]
    home_team = raw.get("homeTeam", {}).get("name", "")
    away_team = raw.get("awayTeam", {}).get("name", "")
    score1, score2, winner = _winner_from_scores(home_team, away_team, ft, pen)
    return {
        "external_id": str(raw.get("id", "")),
        "tournament": "FIFA World Cup 2026",
        "sport": "football",
        "home_team": home_team,
        "away_team": away_team,
        "scheduled_at": raw.get("utcDate"),
        "home_score": score1,
        "away_score": score2,
        "winner": winner,
        "stage": raw.get("stage", ""),
        "venue": raw.get("venue", {}).get("name", ""),
        "is_final": raw.get("status") == "FINISHED",
        "finished_at": raw.get("utcDate") if raw.get("status") == "FINISHED" else None,
    }


def _normalise_fallback(raw: dict) -> dict:
    """Map openfootball 2026 flat format → our DB schema."""
    score = raw.get("score", {}) or {}
    score_ft = score.get("ft")
    score_p = score.get("p")
    team1 = raw.get("team1", "")
    team2 = raw.get("team2", "")
    score1, score2, winner = _winner_from_scores(team1, team2, score_ft, score_p)
    date_str = raw.get("date", "")
    match_num = raw.get("num")
    external_id = f"of_{match_num}" if match_num is not None else f"of_{date_str}_{team1}_{team2}".replace(" ", "_")
    stage = raw.get("group") or raw.get("round", "")
    scheduled_at = date_str or None
    time_str = raw.get("time")
    if date_str and time_str:
        try:
            scheduled_at = f"{date_str}T{time_str.split()[0] if time_str else '12:00:00'}"
        except Exception:
            scheduled_at = date_str
    return {
        "external_id": external_id,
        "tournament": "FIFA World Cup 2026",
        "sport": "football",
        "home_team": team1,
        "away_team": team2,
        "scheduled_at": scheduled_at,
        "home_score": score1,
        "away_score": score2,
        "winner": winner,
        "stage": stage,
        "venue": raw.get("ground", ""),
        "is_final": score1 is not None,
        "finished_at": date_str if score1 is not None else None,
    }


def _is_bracket_placeholder(name: str) -> bool:
    return bool(name) and name.startswith("W") and name[1:].isdigit()


def _match_identity_key(rec: dict) -> str:
    """Stable key for deduping the same fixture across data sources."""
    from backend.trading.market_matcher import _canonical

    home = _canonical(rec.get("home_team") or "") or (rec.get("home_team") or "").strip().lower()
    away = _canonical(rec.get("away_team") or "") or (rec.get("away_team") or "").strip().lower()
    if _is_bracket_placeholder(rec.get("home_team") or "") or _is_bracket_placeholder(rec.get("away_team") or ""):
        return rec.get("external_id") or f"{home}|{away}"
    date = (rec.get("scheduled_at") or "")[:10]
    teams = tuple(sorted([home, away]))
    return f"{date}|{teams[0]}|{teams[1]}"


def _merge_match_records(a: dict, b: dict) -> dict:
    """Merge two records for the same fixture; prefer finalized scores."""
    out = dict(a)
    if b.get("is_final") and not a.get("is_final"):
        out = {**a, **b}
    elif b.get("is_final") and a.get("is_final"):
        out["home_score"] = b.get("home_score") if b.get("home_score") is not None else a.get("home_score")
        out["away_score"] = b.get("away_score") if b.get("away_score") is not None else a.get("away_score")
        aw, bw = a.get("winner"), b.get("winner")
        if bw and bw != "draw":
            out["winner"] = bw
        elif aw and aw != "draw":
            out["winner"] = aw
        else:
            out["winner"] = bw or aw
        out["is_final"] = True
    elif not b.get("is_final") and not a.get("is_final"):
        # Prefer record with more complete schedule/stage info
        if len(b.get("stage") or "") > len(a.get("stage") or ""):
            out["stage"] = b["stage"]
        if b.get("scheduled_at") and not a.get("scheduled_at"):
            out["scheduled_at"] = b["scheduled_at"]
    return out


def _best_final_row(group: list[dict]) -> dict | None:
    """Pick the most authoritative final row (PK winner beats FT draw)."""
    finals = [g for g in group if g.get("is_final")]
    if not finals:
        return None
    for g in finals:
        if g.get("winner") and g.get("winner") != "draw":
            return g
    return finals[0]


async def _propagate_finished_state(db) -> int:
    """If any duplicate fixture row is final, mark siblings final too (settlement fix)."""
    rows = (
        db.table("matches")
        .select("id, home_team, away_team, scheduled_at, is_final, winner, home_score, away_score, external_id")
        .eq("sport", "football")
        .execute()
        .data or []
    )
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(_match_identity_key(r), []).append(r)

    updated = 0
    for group in by_key.values():
        if len(group) < 2:
            continue
        best = _best_final_row(group)
        if not best:
            continue
        payload = {
            "is_final": True,
            "winner": best.get("winner"),
            "home_score": best.get("home_score"),
            "away_score": best.get("away_score"),
            "finished_at": best.get("finished_at"),
        }
        for r in group:
            if (
                r.get("is_final") == payload["is_final"]
                and r.get("winner") == payload["winner"]
                and r.get("home_score") == payload["home_score"]
                and r.get("away_score") == payload["away_score"]
            ):
                continue
            try:
                db.table("matches").update(payload).eq("id", r["id"]).execute()
                updated += 1
            except Exception as exc:
                logger.debug(f"Propagate final state failed for {r['id']}: {exc}")
    if updated:
        logger.info(f"Propagated final state to {updated} duplicate match rows")
    return updated


# ─── Sync to DB ─────────────────────────────────────────────────────────────

async def sync_matches() -> int:
    """Fetch matches from all sources, merge duplicates, upsert into Supabase."""
    db = get_db()
    merged: dict[str, dict] = {}

    def _add(rec: dict, *, prefer_external: str | None = None) -> None:
        key = _match_identity_key(rec)
        if key not in merged:
            merged[key] = rec
            return
        existing = merged[key]
        if prefer_external == "primary" and not str(existing.get("external_id", "")).isdigit():
            rec = {**rec, "external_id": existing.get("external_id")}
        merged[key] = _merge_match_records(existing, rec)

    if settings.wc_api_key:
        try:
            fetcher = WorldCupApiFetcher()
            primary_raw = await fetcher.get_matches()
            for raw in primary_raw:
                if raw:
                    _add(_normalise_primary(raw), prefer_external="primary")
            logger.info(f"Fetched {len(primary_raw)} matches from wc2026api.com")
        except Exception as exc:
            logger.warning(f"Primary WC API failed ({exc})")

    try:
        fallback = OpenfootballFetcher()
        fallback_raw = await fallback.get_matches()
        for raw in fallback_raw:
            if raw:
                _add(_normalise_fallback(raw))
        logger.info(f"Merged {len(fallback_raw)} openfootball fixtures")
    except Exception as exc:
        logger.warning(f"Openfootball fallback failed: {exc}")

    records = list(merged.values())
    if not records:
        logger.error("No WC match data from any source")
        return 0

    result = db.table("matches").upsert(records, on_conflict="external_id").execute()
    count = len(result.data or [])
    await _propagate_finished_state(db)
    logger.info(f"Upserted {count} match records ({len(records)} unique fixtures)")
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
        .select("id, predicted_winner, posted_at, bet_type, bet_line, bet_subject, raw_text")
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

    from backend.sports_data.pick_linking import (
        build_match_index,
        infer_match_candidates,
        pick_best_match,
    )

    by_team, alias_to_canonical = build_match_index(matches)

    linked = 0
    for pick in unlinked:
        candidates = infer_match_candidates(pick, matches, by_team, alias_to_canonical)
        if not candidates:
            continue

        best = pick_best_match(candidates, pick.get("posted_at"), pick.get("raw_text"))
        if not best:
            continue
        try:
            db.table("picks").update({"match_id": best["id"]}).eq("id", pick["id"]).execute()
            linked += 1
        except Exception as exc:
            msg = str(exc)
            if "picks_influencer_match_unique" in msg or "duplicate key" in msg.lower():
                logger.debug(f"Pick {pick['id']} already linked (duplicate influencer/match)")
            else:
                logger.warning(f"Failed to link pick {pick['id']}: {exc}")

    logger.info(f"Linked {linked} picks to matches")
    return linked


async def resolve_pending_picks() -> int:
    """Resolve all pending picks (football + MLB + props) via shared grader."""
    from backend.sports_data.pick_resolver import resolve_all_pending_picks
    return resolve_all_pending_picks()


if __name__ == "__main__":
    asyncio.run(sync_matches())
