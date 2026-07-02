"""
Pickswise.com MLB moneyline picks scraper.

Parses the public /mlb/picks/ hub for "Moneyline - {Team}" lines.
No API key required.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from backend.db import get_db
from backend.sports_data.mlb_fetcher import canonicalise_mlb_team

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BASE = "https://www.pickswise.com"
MLB_PICKS_URL = f"{BASE}/mlb/picks/"

MONEYLINE_RE = re.compile(
    r"Moneyline\s*-\s*([A-Za-z][A-Za-z\s.'-]+?)(?:\s+Bet\b|\s+now\b|\"|$)",
    re.IGNORECASE,
)
VS_RE = re.compile(
    r"([A-Za-z][A-Za-z\s.'-]+?)\s+vs\.?\s+([A-Za-z][A-Za-z\s.'-]+)",
    re.IGNORECASE,
)

PICKSWISE_EXPERTS = ["Jon Picks", "Pickswise Staff"]


class PickswiseScraper:
    def __init__(self):
        self._influencer_cache: dict[str, str] = {}

    def _get_or_create_expert(self, name: str) -> str | None:
        if name in self._influencer_cache:
            return self._influencer_cache[name]
        db = get_db()
        handle = name.lower().replace(" ", "_")
        existing = (
            db.table("influencers")
            .select("id")
            .eq("platform", "pickswise")
            .eq("handle", handle)
            .execute()
            .data
        )
        if existing:
            self._influencer_cache[name] = existing[0]["id"]
            return existing[0]["id"]
        created = (
            db.table("influencers")
            .upsert(
                {
                    "platform": "pickswise",
                    "handle": handle,
                    "display_name": f"{name} (Pickswise)",
                    "profile_url": MLB_PICKS_URL,
                    "is_active": True,
                },
                on_conflict="platform,handle",
            )
            .execute()
            .data
        )
        if created:
            self._influencer_cache[name] = created[0]["id"]
            return created[0]["id"]
        return None

    def _save_pick(
        self,
        influencer_id: str,
        post_id: str,
        raw_text: str,
        predicted_winner: str,
        *,
        confidence: float = 0.55,
    ) -> bool:
        db = get_db()
        try:
            db.table("picks").upsert(
                {
                    "influencer_id": influencer_id,
                    "platform": "pickswise",
                    "post_id": post_id,
                    "raw_text": raw_text[:2000],
                    "predicted_winner": predicted_winner,
                    "confidence": confidence,
                    "bet_type": "moneyline",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending",
                },
                on_conflict="platform,post_id",
            ).execute()
            return True
        except Exception as exc:
            logger.debug(f"Pickswise pick skipped ({post_id}): {exc}")
            return False

    def _parse_picks(self, html: str) -> list[dict]:
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        expert = "Pickswise Staff"
        for name in PICKSWISE_EXPERTS:
            if name in text:
                expert = name
                break

        today = datetime.now(timezone.utc).date().isoformat()
        picks: list[dict] = []
        seen: set[str] = set()

        for m in MONEYLINE_RE.finditer(text):
            team_raw = m.group(1).strip()
            winner = canonicalise_mlb_team(team_raw)
            if not winner or winner in seen:
                continue
            seen.add(winner)
            slug = re.sub(r"[^a-z0-9]+", "_", winner.lower()).strip("_")
            picks.append({
                "expert": expert,
                "predicted_winner": winner,
                "raw_text": f"Pickswise: Moneyline - {team_raw}",
                "post_id": f"pw_mlb_{slug}_{today}",
                "confidence": 0.55,
            })

        return picks

    async def scrape_all(self) -> int:
        total = 0
        async with httpx.AsyncClient() as client:
            for name in PICKSWISE_EXPERTS:
                self._get_or_create_expert(name)

            try:
                r = await client.get(
                    MLB_PICKS_URL, headers=HEADERS, timeout=25, follow_redirects=True,
                )
                if r.status_code != 200:
                    logger.warning(f"Pickswise MLB hub → {r.status_code}")
                    return 0
                parsed = self._parse_picks(r.text)
                logger.info(f"Pickswise: parsed {len(parsed)} MLB moneyline picks")
                for p in parsed:
                    iid = self._get_or_create_expert(p["expert"])
                    if not iid:
                        continue
                    if self._save_pick(
                        iid,
                        p["post_id"],
                        p["raw_text"],
                        p["predicted_winner"],
                        confidence=p.get("confidence") or 0.55,
                    ):
                        total += 1
            except Exception as exc:
                logger.warning(f"Pickswise scrape failed: {exc}")

        logger.info(f"Pickswise: saved {total} picks")
        return total
