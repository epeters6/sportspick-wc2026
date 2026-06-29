"""
Covers.com expert picks scraper.

Scrapes the World Cup 2026 daily expert pick pages — one per group (A–L).
Each page has named Covers staff experts (Sam Farley, Emanuel Rosu, etc.) who
make consistent picks that we can track for accuracy over time.

The page structure uses prose sections:
  <h2>Mexico vs South Africa prediction</h2>
  <p>...analysis...</p>
  <p>Back Mexico to win by -1.5...</p>
  <p>Check out Sam Farley's full Mexico vs. South Africa predictions!</p>

No API key required — public HTML pages.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

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

BASE = "https://www.covers.com"

# 2026 WC has 12 groups (48 teams, expanded format)
GROUPS = list("ABCDEFGHIJKL")
GROUP_URL = "https://www.covers.com/world-cup/group-{group}-daily-expert-picks-2026"

# Known Covers experts — pre-seed them as influencers
COVERS_EXPERTS = [
    # Core soccer staff
    "Chris Vasile",    # 13+ yrs, soccer specialist, also runs Game Day Wagers YT
    "Sam Farley",      # UK-based, soccer & NFL; also writes for ActionNetwork
    "Jason Ence",      # writes daily WC match articles
    "Emanuel Rosu",    # Bucharest-based, Guardian/BBC contributor
    "James Eastham",   # 20+ yrs, France/UK, Guardian/Betfair/ESPN
    "Chris Gregory",   # Publishing Editor, writes group pick pages
    "Tom Oldfield",
    "John Ryan",
    "R.J. White",
    "Kyle LaRusic",
    "Eric Rasimowicz",
    # Additional Covers soccer/WC contributors
    "Matt McEwan",
    "Dan Kilpatrick",
    "Joe Osborne",
    "Wil Burrows",
    "Alex Selvig",
    "Drew Davison",
    "Nick Raffoul",
    "Adam Thompson",
    "Levi Buckley",
    "Warren Sharp",
    "Dave Cokin",
    "Bruce Marshall",
    "Phil Naessens",   # Covers MLB moneyline picks author
]

# Maps Covers.com team names → canonical DB names (openfootball names)
# Covers uses some different names than openfootball
COVERS_TEAM_MAP: dict[str, str] = {
    # Direct matches (lowercase key)
    "mexico": "Mexico",
    "south africa": "South Africa",
    "south korea": "South Korea",
    "czechia": "Czech Republic",
    "czech republic": "Czech Republic",
    "usa": "USA",
    "united states": "USA",
    "u.s.": "USA",
    "brazil": "Brazil",
    "argentina": "Argentina",
    "france": "France",
    "england": "England",
    "germany": "Germany",
    "spain": "Spain",
    "portugal": "Portugal",
    "netherlands": "Netherlands",
    "holland": "Netherlands",
    "italy": "Italy",
    "canada": "Canada",
    "japan": "Japan",
    "australia": "Australia",
    "morocco": "Morocco",
    "senegal": "Senegal",
    "nigeria": "Nigeria",
    "ghana": "Ghana",
    "colombia": "Colombia",
    "chile": "Chile",
    "uruguay": "Uruguay",
    "ecuador": "Ecuador",
    "saudi arabia": "Saudi Arabia",
    "iran": "Iran",
    "qatar": "Qatar",
    "croatia": "Croatia",
    "switzerland": "Switzerland",
    "belgium": "Belgium",
    "denmark": "Denmark",
    "sweden": "Sweden",
    "poland": "Poland",
    "serbia": "Serbia",
    "ukraine": "Ukraine",
    "austria": "Austria",
    "norway": "Norway",
    "scotland": "Scotland",
    "turkey": "Turkey",
    "iraq": "Iraq",
    "panama": "Panama",
    "haiti": "Haiti",
    "new zealand": "New Zealand",
    "cape verde": "Cape Verde",
    "ivory coast": "Ivory Coast",
    "côte d'ivoire": "Ivory Coast",
    "dr congo": "DR Congo",
    "democratic republic of congo": "DR Congo",
    "algeria": "Algeria",
    "jordan": "Jordan",
    "uzbekistan": "Uzbekistan",
    "paraguay": "Paraguay",
    "curacao": "Curaçao",
    "curaçao": "Curaçao",
    "egypt": "Egypt",
    "tunisia": "Tunisia",
    "bosnia": "Bosnia & Herzegovina",
    "bosnia and herzegovina": "Bosnia & Herzegovina",
    "bosnia & herzegovina": "Bosnia & Herzegovina",
    "venezuela": "Venezuela",
    "peru": "Peru",
    "bolivia": "Bolivia",
}


class CoversScraper:
    def __init__(self):
        self._influencer_cache: dict[str, str] = {}

    async def _get_or_create_expert(self, name: str) -> str | None:
        if name in self._influencer_cache:
            return self._influencer_cache[name]
        db = get_db()
        handle = name.lower().replace(" ", "_").replace(".", "")
        existing = (
            db.table("influencers")
            .select("id")
            .eq("platform", "covers")
            .eq("handle", handle)
            .execute()
            .data
        )
        if existing:
            self._influencer_cache[name] = existing[0]["id"]
            return existing[0]["id"]

        created = (
            db.table("influencers")
            .upsert({
                "platform": "covers",
                "handle": handle,
                "display_name": f"{name} (Covers.com)",
                "profile_url": f"https://www.covers.com/author/{handle}",
                "is_active": True,
            }, on_conflict="platform,handle")
            .execute()
            .data
        )
        if created:
            self._influencer_cache[name] = created[0]["id"]
            return created[0]["id"]
        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _fetch_group_page(self, client: httpx.AsyncClient, group: str) -> str:
        url = GROUP_URL.format(group=group.lower())
        r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        if r.status_code == 404:
            return ""
        r.raise_for_status()
        return r.text

    def _parse_picks_from_html(self, html: str, group: str) -> list[dict]:
        """
        Extract picks from the group page.

        The page structure is prose-based:
          <h2>Team1 vs Team2 prediction</h2>
          <p>...analysis paragraphs...</p>
          <p>Back Team1 to win...</p>
          <p><em>Check out {Expert}'s full <a>Team1 vs. Team2 predictions</a>!</em></p>

        IMPORTANT: The attribution line has the match name inside a <a> tag, so the
        full text is split across NavigableStrings. We must use p.get_text() to read
        the whole paragraph text before pattern matching.
        """
        soup = BeautifulSoup(html, "lxml")
        picks = []
        now_iso = datetime.now(timezone.utc).isoformat()

        # Pattern matches the full text of an attribution paragraph
        attribution_pattern = re.compile(
            r"Check out ([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)'s full (.+?) vs\.? (.+?) predictions?",
            re.IGNORECASE,
        )

        # Search every paragraph (get_text handles inline tags like <a> and <em>)
        for para in soup.find_all("p"):
            full_text = para.get_text(separator=" ", strip=True)
            m = attribution_pattern.search(full_text)
            if not m:
                continue

            expert_name = m.group(1).strip()
            team1_raw = m.group(2).strip()
            team2_raw = m.group(3).strip().rstrip("!. ")

            # Walk back through preceding siblings to gather recommendation text
            section_parts = []
            current = para.find_previous_sibling()
            for _ in range(6):
                if current is None:
                    break
                tag = getattr(current, "name", "")
                if tag in ("h1", "h2", "h3"):
                    break
                text = current.get_text(separator=" ", strip=True)
                if text and "Check out" not in text:
                    section_parts.insert(0, text)
                current = current.find_previous_sibling()

            section_text = " ".join(section_parts)

            from backend.scrapers.pick_extractor import TEAM_ALIASES, extract_all_picks

            canonical1 = _canonicalize_covers_team(team1_raw) or TEAM_ALIASES.get(team1_raw.lower())
            canonical2 = _canonicalize_covers_team(team2_raw) or TEAM_ALIASES.get(team2_raw.lower())
            allowed = {t for t in (canonical1, canonical2) if t}

            extracted = extract_all_picks(section_text, allowed_teams=allowed or None)
            if not extracted:
                predicted_winner = _extract_winner_from_section(
                    section_text, team1_raw, team2_raw
                )
                if predicted_winner:
                    canonical = _canonicalize_covers_team(predicted_winner) or TEAM_ALIASES.get(
                        predicted_winner.lower()
                    )
                    if canonical:
                        extracted = [{
                            "predicted_winner": canonical,
                            "bet_type": "draw" if canonical == "draw" else "moneyline",
                            "bet_line": None,
                            "confidence": 0.72,
                            "predicted_score": None,
                        }]

            for j, pick_data in enumerate(extracted):
                canonical = pick_data.get("predicted_winner")
                if not canonical:
                    continue
                bet_type = pick_data.get("bet_type") or "moneyline"
                post_id = (
                    f"covers_{group}_{_slugify(team1_raw)}_{_slugify(team2_raw)}"
                    f"_{_slugify(expert_name)}_{bet_type}_{j}"
                )
                raw_text = (
                    f"Group {group}: {team1_raw} vs {team2_raw} "
                    f"({expert_name}): {section_text[:400]}"
                )
                picks.append({
                    "expert": expert_name,
                    "raw_text": raw_text,
                    "predicted_winner": canonical,
                    "bet_type": bet_type,
                    "bet_line": pick_data.get("bet_line"),
                    "bet_subject": pick_data.get("bet_subject"),
                    "confidence": pick_data.get("confidence") or 0.72,
                    "posted_at": now_iso,
                    "post_id": post_id,
                })

        return picks

    def _mlb_daily_urls(self) -> list[str]:
        """Today's Covers MLB moneyline article + hub fallback."""
        now = datetime.now(timezone.utc)
        weekday = now.strftime("%A").lower()
        month = now.month
        day = now.day
        year = now.year
        return [
            f"{BASE}/mlb/moneyline-picks-{weekday}-{month}-{day}-{year}",
            f"{BASE}/picks/mlb",
        ]

    def _parse_mlb_picks_from_html(self, html: str, source_url: str) -> list[dict]:
        """
        Parse Covers daily MLB moneyline article.

        Structure:
          ### White Sox vs Tigers: White Sox (+113)
          ...analysis...
        """
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
        expert = "Phil Naessens"
        for name in COVERS_EXPERTS:
            if name in text:
                expert = name
                break

        now_iso = datetime.now(timezone.utc).isoformat()
        picks: list[dict] = []

        # h3 headings: "White Sox vs Tigers: White Sox (+113)"
        heading_re = re.compile(
            r"(?:^|\n)\s*(?:#{1,3}\s*)?"
            r"(.+?)\s+vs\.?\s+(.+?):\s*(.+?)\s*\([+\-]?\d+\)",
            re.IGNORECASE | re.MULTILINE,
        )
        for m in heading_re.finditer(text):
            team1_raw = m.group(1).strip()
            team2_raw = m.group(2).strip()
            pick_raw = m.group(3).strip()
            winner = canonicalise_mlb_team(pick_raw) or canonicalise_mlb_team(team1_raw)
            if not winner:
                if pick_raw.lower() in team1_raw.lower():
                    winner = canonicalise_mlb_team(team1_raw)
                elif pick_raw.lower() in team2_raw.lower():
                    winner = canonicalise_mlb_team(team2_raw)
            if not winner:
                continue
            slug = f"{_slugify(team1_raw)}_{_slugify(team2_raw)}_{_slugify(winner)}"
            picks.append({
                "expert": expert,
                "raw_text": f"MLB: {team1_raw} vs {team2_raw} — pick {pick_raw} ({expert})",
                "predicted_winner": winner,
                "bet_type": "moneyline",
                "bet_line": None,
                "confidence": 0.70,
                "posted_at": now_iso,
                "post_id": f"covers_mlb_{slug}_{now_iso[:10]}",
                "post_url": source_url,
            })

        return picks

    async def _scrape_mlb_picks(self, client: httpx.AsyncClient) -> int:
        """Fetch today's Covers MLB moneyline picks."""
        saved = 0
        db = get_db()
        for expert in COVERS_EXPERTS:
            await self._get_or_create_expert(expert)

        for url in self._mlb_daily_urls():
            try:
                r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                picks = self._parse_mlb_picks_from_html(r.text, url)
                if not picks:
                    continue
                logger.info(f"Covers MLB ({url}): {len(picks)} picks parsed")
                for pick in picks:
                    influencer_id = await self._get_or_create_expert(pick["expert"])
                    if not influencer_id:
                        continue
                    record = {
                        "influencer_id": influencer_id,
                        "platform": "covers",
                        "post_id": pick["post_id"],
                        "post_url": pick.get("post_url", url),
                        "raw_text": pick["raw_text"],
                        "predicted_winner": pick["predicted_winner"],
                        "predicted_score": None,
                        "confidence": pick["confidence"],
                        "bet_type": pick.get("bet_type") or "moneyline",
                        "bet_line": pick.get("bet_line"),
                        "bet_subject": pick.get("bet_subject"),
                        "posted_at": pick["posted_at"],
                    }
                    try:
                        db.table("picks").upsert(
                            record, on_conflict="platform,post_id"
                        ).execute()
                        saved += 1
                    except Exception as exc:
                        logger.warning(f"Failed to save Covers MLB pick: {exc}")
                if saved:
                    break
                await asyncio.sleep(1.5)
            except Exception as exc:
                logger.warning(f"Covers MLB fetch failed ({url}): {exc}")
        return saved

    async def scrape_all(self) -> int:
        db = get_db()
        for expert in COVERS_EXPERTS:
            await self._get_or_create_expert(expert)

        total = 0
        async with httpx.AsyncClient() as client:
            for group in GROUPS:
                try:
                    html = await self._fetch_group_page(client, group)
                    if not html:
                        continue
                    picks = self._parse_picks_from_html(html, group)
                    logger.info(f"Covers Group {group}: {len(picks)} picks parsed")

                    for pick in picks:
                        expert_name = pick.get("expert") or "Covers Staff"
                        influencer_id = await self._get_or_create_expert(expert_name)
                        if not influencer_id:
                            continue
                        record = {
                            "influencer_id": influencer_id,
                            "platform": "covers",
                            "post_id": pick["post_id"],
                            "post_url": GROUP_URL.format(group=group.lower()),
                            "raw_text": pick["raw_text"],
                            "predicted_winner": pick["predicted_winner"],
                            "predicted_score": None,
                            "confidence": pick["confidence"],
                            "bet_type": pick.get("bet_type") or "moneyline",
                            "bet_line": pick.get("bet_line"),
                        "bet_subject": pick.get("bet_subject"),
                            "posted_at": pick["posted_at"],
                        }
                        try:
                            db.table("picks").upsert(
                                record, on_conflict="platform,post_id"
                            ).execute()
                            total += 1
                        except Exception as exc:
                            logger.warning(f"Failed to save Covers pick: {exc}")

                    await asyncio.sleep(1.5)
                except Exception as exc:
                    logger.warning(f"Covers Group {group} failed: {exc}")

            mlb_saved = await self._scrape_mlb_picks(client)
            total += mlb_saved
            if mlb_saved:
                logger.info(f"Covers MLB: saved {mlb_saved} picks")

        logger.info(f"Covers.com: saved {total} expert picks")
        return total


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_winner_from_section(text: str, team1: str, team2: str) -> str | None:
    """
    Given prose text and two team names, determine which team the expert picked.
    Returns the team name string or "draw" or None.
    """
    if not text:
        return None

    text_lower = text.lower()
    t1 = team1.lower()
    t2 = team2.lower()

    # Check for draw
    if re.search(r"\bfavor\s+(?:a\s+)?draw\b|expect\s+(?:a\s+)?draw\b|going\s+with\s+(?:a\s+)?draw\b", text_lower):
        return "draw"

    # Positive pick signals followed by team name
    for sig in ["back", "backing", "favor", "favoring", "pick", "take", "going with"]:
        pattern = rf"\b{re.escape(sig)}\s+({re.escape(t1)}|{re.escape(t2)})"
        mm = re.search(pattern, text_lower)
        if mm:
            return team1 if mm.group(1) == t1 else team2

    # Team name followed by win signals
    for team_lower, team_orig in [(t1, team1), (t2, team2)]:
        if re.search(
            rf"\b{re.escape(team_lower)}\s+(?:to win|wins?\b|will win|are (?:the )?(?:clear )?favorites?)",
            text_lower,
        ):
            return team_orig
        # "Back {team}" / "{team} -1.5" / "{team} ML"
        if re.search(
            rf"\bback\s+{re.escape(team_lower)}|{re.escape(team_lower)}\s+[-+]\d|{re.escape(team_lower)}\s+ml\b",
            text_lower,
        ):
            return team_orig

    # Broader fallback: team mentioned near pick/prediction/winner keywords
    for team_lower, team_orig in [(t1, team1), (t2, team2)]:
        idx = text_lower.find(team_lower)
        if idx >= 0:
            window = text_lower[max(0, idx - 50): idx + 80]
            if any(kw in window for kw in ["pick", "predict", "winner", "win", "back", "favor", "take"]):
                return team_orig

    return None


def _canonicalize_covers_team(name: str) -> str | None:
    """Map a Covers team name to the canonical DB name."""
    if not name:
        return None
    key = name.strip().lower()
    return COVERS_TEAM_MAP.get(key)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]
