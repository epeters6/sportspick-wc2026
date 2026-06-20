"""
ActionNetwork.com expert picks scraper.

Strategy:
  ActionNetwork renders its listing page via JavaScript, so we cannot discover
  articles by scraping /soccer. Instead we:
  1. Pull today's + recent WC matches from our DB.
  2. Construct the expected ActionNetwork article URL for each match — they use
     a very consistent pattern:
       /soccer/{team1}-vs-{team2}-prediction-pick-odds-world-cup-{weekday}-{month}-{day}
  3. Try a few URL variants until one returns 2xx.
  4. Parse the article for the author and explicit "#### Pick:" lines.

No API key required — public HTML pages.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

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

# Explicit pick-line patterns (ActionNetwork uses both h4 and list-item formats)
PICK_LINE_RE = re.compile(
    r"(?:#{1,4}|-|\*)\s*Pick:\s*(.+)|Our (?:best |top )?bet[:\s]+(.+)",
    re.IGNORECASE,
)

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

    def _save_pick(self, influencer_id: str, post_id: str, raw_text: str, predicted_winner: str) -> bool:
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
            if 200 <= r.status_code < 300:
                return r.text
            logger.warning(f"ActionNetwork {url} → {r.status_code}")
            return None
        except Exception as exc:
            logger.warning(f"ActionNetwork fetch error {url}: {exc}")
            return None

    # ── Article URL construction from match schedule ───────────────────────────

    @staticmethod
    def _team_slug(team_name: str) -> str:
        """Convert a canonical team name to an ActionNetwork URL slug."""
        overrides = {
            # ActionNetwork-specific spellings (verified from actual URLs)
            "USA": "usa",
            "Türkiye": "turkiye",
            "Turkey": "turkiye",
            "Bosnia-Herzegovina": "bosnia-herzegovina",
            "Bosnia & Herzegovina": "bosnia-herzegovina",
            "Ivory Coast": "ivory-coast",
            "DR Congo": "dr-congo",
            "South Korea": "south-korea",
            "Saudi Arabia": "saudi-arabia",
            "New Zealand": "new-zealand",
            "Czech Republic": "czechia",
            "Costa Rica": "costa-rica",
            "Trinidad & Tobago": "trinidad-tobago",
            "Curaçao": "curacao",
        }
        if team_name in overrides:
            return overrides[team_name]
        # Normalise unicode (ç → c, ü → u, etc.) before slugifying
        import unicodedata
        normalised = unicodedata.normalize("NFKD", team_name).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", "-", normalised.lower()).strip("-")

    def _build_article_urls(self, home: str, away: str, match_date: datetime) -> list[str]:
        """
        Generate candidate ActionNetwork article URLs for a given match.
        AN uses several slightly different patterns — we try all of them.
        """
        h = self._team_slug(home)
        a = self._team_slug(away)
        weekday = match_date.strftime("%A").lower()     # e.g. "friday"
        month = match_date.strftime("%B").lower()       # e.g. "june"
        day = str(match_date.day)                       # e.g. "19"

        templates = [
            f"{BASE}/soccer/{h}-vs-{a}-prediction-pick-odds-world-cup-{weekday}-{month}-{day}",
            f"{BASE}/soccer/{h}-vs-{a}-prediction-pick-world-cup-odds-{weekday}-{month}-{day}",
            f"{BASE}/soccer/{h}-vs-{a}-prediction-pick-world-cup-{weekday}-{month}-{day}",
            f"{BASE}/soccer/{h}-vs-{a}-predictions-lineups-odds-world-cup-{weekday}-{month}-{day}",
            f"{BASE}/soccer/{h}-vs-{a}-prediction-lineups-odds-world-cup-{weekday}-{month}-{day}",
        ]
        return templates

    def _get_recent_matches(self, days_back: int = 4) -> list[dict]:
        """Fetch WC matches from the last N days + the next day."""
        db = get_db()
        cutoff_past = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        cutoff_future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        rows = (
            db.table("matches")
            .select("id, home_team, away_team, scheduled_at")
            .gte("scheduled_at", cutoff_past)
            .lte("scheduled_at", cutoff_future)
            .execute()
            .data or []
        )
        return rows

    # ── Article parsing ────────────────────────────────────────────────────────

    def _extract_author(self, soup: BeautifulSoup) -> str:
        """Return the article author name, falling back to known AN experts."""
        # Try structured author tags first
        for candidate in [
            soup.find("span", {"data-testid": "author-name"}),
            soup.find(class_=re.compile(r"author|byline", re.I)),
            soup.find("a", href=re.compile(r"/authors?/|/@")),
        ]:
            if candidate:
                name = candidate.get_text(strip=True)
                if len(name) > 2:
                    return name

        # Scan plain text for known expert names right after the h1
        text = soup.get_text(" ")
        for name in ACTION_NETWORK_EXPERTS:
            if name in text:
                return name

        return "Action Network Staff"

    def _extract_projected_winner(self, soup: BeautifulSoup) -> str | None:
        """
        ActionNetwork articles always include a 'Projected Chance of Winning' table:
            | Croatia | Draw | England |
            |---------|------|---------|
            | 19.3%   | 23.9%| 56.9%  |

        Parse it and return the team with the highest projected win probability.
        Only returns a winner if one team is clearly ahead (>40%).
        """
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            if len(headers) != 3:
                continue
            # Middle header should be "Draw"
            if "draw" not in headers[1].lower():
                continue

            rows = table.find_all("tr")
            # Find the data row with percentages
            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) != 3:
                    continue
                try:
                    pcts = [
                        float(c.get_text(strip=True).replace("%", "").strip())
                        for c in cells
                    ]
                except ValueError:
                    continue
                # pcts[0]=team1, pcts[1]=draw, pcts[2]=team2
                if pcts[0] > pcts[2] and pcts[0] > 40:
                    return _canonicalise_team(headers[0])
                if pcts[2] > pcts[0] and pcts[2] > 40:
                    return _canonicalise_team(headers[2])
                # If very close (both <40 / draw game), skip — don't force a pick
        return None

    def _parse_article(self, html: str, url: str) -> list[dict]:
        """
        Parse an ActionNetwork article and return a list of pick dicts:
        {expert, predicted_winner, raw_text, post_id, published_at}

        Priority:
        1. Explicit moneyline/winner pick lines (- Pick: {Team}, #### Pick: {Team})
        2. Projected Chance of Winning table (model projection attributed to expert)
        """
        soup = BeautifulSoup(html, "lxml")

        expert = self._extract_author(soup)
        time_tag = soup.find("time")
        published_at = time_tag["datetime"] if time_tag and time_tag.get("datetime") else None
        slug = url.rstrip("/").split("/")[-1][:80]
        full_text = soup.get_text(" ", strip=True)

        results = []

        # Phase 1: explicit "Pick:" lines in article text
        for idx, groups in enumerate(PICK_LINE_RE.finditer(full_text)):
            raw_pick_line = groups.group(1) or groups.group(2) or ""
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

        # Phase 2: if no winner pick found, use win-probability projection table
        if not results:
            winner = self._extract_projected_winner(soup)
            if winner:
                post_id = f"an_{slug}_proj"
                results.append({
                    "expert": expert,
                    "predicted_winner": winner,
                    "raw_text": f"{url}\nProjected winner: {winner}",
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
        """
        For each recent WC match, construct candidate ActionNetwork article URLs
        and scrape the first one that returns a valid response.
        """
        total = 0
        async with httpx.AsyncClient() as client:
            await self._seed_experts()

            matches = self._get_recent_matches(days_back=4)
            logger.info(f"ActionNetwork: checking {len(matches)} recent matches")

            for match in matches:
                scheduled_raw = match.get("scheduled_at", "")
                try:
                    match_date = datetime.fromisoformat(
                        scheduled_raw.replace("Z", "+00:00")
                    )
                except Exception:
                    continue

                home = match.get("home_team", "")
                away = match.get("away_team", "")
                candidate_urls = self._build_article_urls(home, away, match_date)

                article_html = None
                used_url = None
                for url in candidate_urls:
                    await asyncio.sleep(1.0)
                    html = await self._fetch(client, url)
                    if html:
                        article_html = html
                        used_url = url
                        break

                if not article_html:
                    logger.debug(f"ActionNetwork: no article found for {home} vs {away}")
                    continue

                picks = self._parse_article(article_html, used_url)
                if not picks:
                    logger.debug(f"ActionNetwork: no picks parsed from {used_url}")
                    continue

                for p in picks:
                    iid = self._get_or_create_expert(p["expert"])
                    if not iid:
                        continue
                    saved = self._save_pick(
                        iid,
                        p["post_id"],
                        p["raw_text"],
                        p["predicted_winner"],
                    )
                    if saved:
                        total += 1

        logger.info(f"ActionNetwork: scraped {total} new picks")
        return total
