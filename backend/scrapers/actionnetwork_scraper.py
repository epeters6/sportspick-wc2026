"""
ActionNetwork.com expert picks scraper.

Strategy:
1. Fetch the /soccer listing page to discover recent WC2026 prediction articles.
2. Each article is written by a named expert (William Boor, Sam Farley, etc.).
3. Inside each article, find explicit moneyline picks:
   - "#### Pick: {Team} Moneyline" patterns
   - Table rows with "Pick" column containing a team name
4. Each expert becomes a separate influencer; one pick per (expert, match).

No API key required — public HTML pages.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.db import get_db
from backend.scrapers.pick_extractor import TEAM_ALIASES, extract_pick

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BASE = "https://www.actionnetwork.com"
LISTING_URL = "https://www.actionnetwork.com/soccer"

# Only ingest free (non-PRO) articles that match these URL patterns
WC_URL_PATTERNS = [
    r"/soccer/[a-z-]+-vs-[a-z-]+-prediction",
    r"/soccer/world-cup-best-bets",
    r"/soccer/world-cup-picks",
]

# Explicit pick-line pattern: "#### Pick: {anything}"
PICK_LINE_RE = re.compile(r"####\s*Pick:\s*(.+)", re.IGNORECASE)

# Known ActionNetwork soccer experts — pre-seeded as influencers
ACTION_NETWORK_EXPERTS = [
    "William Boor",
    "Sam Farley",
    "Nick Giffen",
    "Alex Kolodziej",
    "Stefano Fusaro",
    "Carlos Avilan",
    "Evan Abrams",
    "Steven Petrella",
    "Sean Koer",
    "Stephen Kamph",
    "Action Network Staff",
]


def _canonicalise_team(raw: str) -> str | None:
    """Map a raw team string to canonical DB name via TEAM_ALIASES."""
    cleaned = raw.strip().lower()
    # Direct alias lookup
    if cleaned in TEAM_ALIASES:
        return TEAM_ALIASES[cleaned]
    # Try multi-word partial: strip trailing words one at a time
    parts = cleaned.split()
    for length in range(len(parts), 0, -1):
        phrase = " ".join(parts[:length])
        if phrase in TEAM_ALIASES:
            return TEAM_ALIASES[phrase]
    return None


def _extract_team_from_pick_line(line: str) -> str | None:
    """
    Given a '#### Pick: ...' line, extract the backing team if it's a
    moneyline/winner pick (not spread/total/BTTS).

    Examples that resolve:
        "Portugal Moneyline (-350)"  → "Portugal"
        "Ghana -0.25 (-109)"         → "Ghana"   (team backed on spread)
        "USA to Win"                 → "USA"

    Examples that return None:
        "Under 2.5 Goals (-138)"
        "Colombia Scores in Both Halves (+125)"
        "+475 Draw"
    """
    line = line.strip()
    # Skip obvious totals / BTTS / draw-only picks
    skip = re.compile(
        r"under|over|both teams|btts|draw|total goals|both halves|"
        r"anytime|first goal|last goal|correct score",
        re.IGNORECASE,
    )
    if skip.search(line):
        return None

    # Strip odds in parentheses/brackets and common suffixes
    cleaned = re.sub(r"\([\+\-]?\d+\)", "", line)
    cleaned = re.sub(r"moneyline|ml|to win|asian handicap|\-\d+\.?\d*|\+\d+\.?\d*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,.-")

    return _canonicalise_team(cleaned)


class ActionNetworkScraper:
    """Scrapes ActionNetwork soccer prediction articles for WC2026 expert picks."""

    def __init__(self):
        self._influencer_cache: dict[str, str] = {}  # expert_name → DB id

    # ── DB helpers ─────────────────────────────────────────────────────────────

    def _get_or_create_expert(self, expert_name: str) -> str | None:
        """Return (or create) the influencer DB id for this expert."""
        if expert_name in self._influencer_cache:
            return self._influencer_cache[expert_name]

        db = get_db()
        handle = expert_name.lower().replace(" ", "_")
        existing = (
            db.table("influencers")
            .select("id")
            .eq("platform", "actionnetwork")
            .eq("handle", handle)
            .execute()
            .data
        )
        if existing:
            iid = existing[0]["id"]
        else:
            profile_url = f"{BASE}/author/{handle}"
            row = (
                db.table("influencers")
                .upsert(
                    {
                        "platform": "actionnetwork",
                        "handle": handle,
                        "display_name": expert_name,
                        "profile_url": profile_url,
                        "follower_count": 0,
                        "is_active": True,
                        "added_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="platform,handle",
                )
                .execute()
                .data
            )
            iid = row[0]["id"] if row else None

        if iid:
            self._influencer_cache[expert_name] = iid
        return iid

    def _save_pick(self, influencer_id: str, post_id: str, raw_text: str, predicted_winner: str, published_at: str | None) -> bool:
        """Insert a pick, skip on duplicate (influencer+post or influencer+match)."""
        db = get_db()
        try:
            db.table("picks").upsert(
                {
                    "influencer_id": influencer_id,
                    "platform": "actionnetwork",
                    "post_id": post_id,
                    "raw_text": raw_text[:2000],
                    "predicted_winner": predicted_winner,
                    "predicted_score": None,
                    "confidence": None,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "published_at": published_at,
                    "status": "pending",
                },
                on_conflict="platform,post_id",
            ).execute()
            return True
        except Exception as exc:
            logger.debug(f"Pick insert skipped ({post_id}): {exc}")
            return False

    # ── HTTP ───────────────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> str | None:
        try:
            r = await client.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            logger.warning(f"ActionNetwork {url} → {r.status_code}")
            return None
        except Exception as exc:
            logger.warning(f"ActionNetwork fetch error {url}: {exc}")
            return None

    # ── Article discovery ──────────────────────────────────────────────────────

    def _discover_article_urls(self, html: str) -> list[str]:
        """Extract WC2026 prediction article URLs from the /soccer listing page."""
        soup = BeautifulSoup(html, "lxml")
        urls = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("/soccer/"):
                continue
            if "world-cup" not in href:
                continue
            # Skip PRO-gated model projection articles — they 403 without a subscription
            if "model-projections" in href or "betting-edges" in href:
                continue
            full = BASE + href if href.startswith("/") else href
            if full not in urls:
                urls.append(full)
        return urls

    # ── Article parsing ────────────────────────────────────────────────────────

    def _parse_article(self, html: str, url: str) -> list[dict]:
        """
        Parse an ActionNetwork article and return a list of pick dicts:
        {expert, predicted_winner, raw_text, post_id, published_at}
        """
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Extract author
        author_tag = (
            soup.find("span", {"data-testid": "author-name"})
            or soup.find(class_=re.compile(r"author|byline", re.I))
            or soup.find("a", href=re.compile(r"/authors?/"))
        )
        expert = author_tag.get_text(strip=True) if author_tag else "Action Network Staff"
        # Normalise "Action Network Staff" variants
        if not expert or len(expert) < 3:
            expert = "Action Network Staff"

        # Published date
        time_tag = soup.find("time")
        published_at = time_tag["datetime"] if time_tag and time_tag.get("datetime") else None

        # Article slug as base post_id
        slug = url.rstrip("/").split("/")[-1][:80]

        # Find all "#### Pick:" lines in raw text
        full_text = soup.get_text(" ", strip=True)
        pick_line_matches = PICK_LINE_RE.findall(full_text)

        for idx, raw_pick_line in enumerate(pick_line_matches):
            winner = _extract_team_from_pick_line(raw_pick_line)
            if not winner:
                continue
            post_id = f"an_{slug}_{idx}"
            results.append({
                "expert": expert,
                "predicted_winner": winner,
                "raw_text": f"{url}\n{raw_pick_line}",
                "post_id": post_id,
                "published_at": published_at,
            })

        # Fallback: if no explicit Pick lines, try table-based picks
        if not results:
            for table in soup.find_all("table"):
                headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
                if "pick" not in headers:
                    continue
                pick_col = headers.index("pick")
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all(["td", "th"])
                    if len(cells) <= pick_col:
                        continue
                    pick_text = cells[pick_col].get_text(strip=True)
                    winner = _extract_team_from_pick_line(pick_text)
                    if not winner:
                        continue
                    post_id = f"an_{slug}_t{pick_col}"
                    results.append({
                        "expert": expert,
                        "predicted_winner": winner,
                        "raw_text": f"{url}\n{pick_text}",
                        "post_id": post_id,
                        "published_at": published_at,
                    })

        return results

    # ── Pre-seed experts ───────────────────────────────────────────────────────

    async def _seed_experts(self) -> None:
        """Ensure all known AN experts exist in the DB."""
        for name in ACTION_NETWORK_EXPERTS:
            self._get_or_create_expert(name)

    # ── Main entry ─────────────────────────────────────────────────────────────

    async def scrape_all(self) -> int:
        """Discover and scrape ActionNetwork WC2026 prediction articles."""
        total = 0
        async with httpx.AsyncClient() as client:
            await self._seed_experts()

            listing_html = await self._fetch(client, LISTING_URL)
            if not listing_html:
                logger.warning("ActionNetwork: could not fetch listing page")
                return 0

            article_urls = self._discover_article_urls(listing_html)
            logger.info(f"ActionNetwork: found {len(article_urls)} WC article URLs")

            for url in article_urls:
                await asyncio.sleep(1.5)  # polite delay
                article_html = await self._fetch(client, url)
                if not article_html:
                    continue

                picks = self._parse_article(article_html, url)
                for p in picks:
                    iid = self._get_or_create_expert(p["expert"])
                    if not iid:
                        continue
                    saved = self._save_pick(
                        iid,
                        p["post_id"],
                        p["raw_text"],
                        p["predicted_winner"],
                        p["published_at"],
                    )
                    if saved:
                        total += 1

        logger.info(f"ActionNetwork: scraped {total} new picks")
        return total
