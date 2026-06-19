"""
Twitter / X scraper using twikit (cookie-based, free, unofficial).

twikit uses your own account's cookies so it does NOT require an API key.
Set TWITTER_AUTH_TOKEN and TWITTER_CT0 in .env — grab them from browser
DevTools → Application → Cookies → twitter.com after logging in.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    from twikit import Client as TwikitClient
except Exception as _twikit_import_err:
    import sys
    print(f"[twikit import failed] {type(_twikit_import_err).__name__}: {_twikit_import_err}", file=sys.stderr)
    TwikitClient = None  # type: ignore

from backend.config import get_settings
from backend.db import get_db
from backend.scrapers.pick_extractor import extract_pick

settings = get_settings()

# Sports-pick related keywords used to filter tweets
PICK_KEYWORDS = [
    "prediction", "predict", "pick", "winner", "final score",
    "bet", "wager", "odds", "💰", "🏆", "#worldcup", "#wc2026",
    "going to win", "will win", "beat", "over", "under",
]

# Max tweets to fetch per influencer per scrape cycle
TWEETS_PER_USER = 20


class TwitterScraper:
    def __init__(self):
        if TwikitClient is None:
            raise RuntimeError("twifork is not installed — run: pip install twifork[impersonate]")
        try:
            self.client = TwikitClient(language="en-US", impersonate="chrome124")
        except TypeError:
            self.client = TwikitClient(language="en-US")
        self._authenticated = False

    async def authenticate(self) -> None:
        if self._authenticated:
            return
        if not settings.twitter_auth_token or not settings.twitter_ct0:
            logger.warning("Twitter cookies not configured — scraper disabled")
            return
        try:
            # set_cookies is synchronous in twikit 2.x
            self.client.set_cookies({
                "auth_token": settings.twitter_auth_token,
                "ct0": settings.twitter_ct0,
            })
            self._authenticated = True
            logger.info("Twitter: authenticated via cookies")
        except Exception as exc:
            logger.error(f"Twitter auth failed: {exc}")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
    async def fetch_user_tweets(self, handle: str) -> list[dict]:
        await self.authenticate()
        if not self._authenticated:
            return []
        try:
            user = await self.client.get_user_by_screen_name(handle)
            tweets = await user.get_tweets("Tweets", count=TWEETS_PER_USER)
            results = []
            for t in tweets:
                text = t.full_text or t.text or ""
                if not _is_pick_post(text):
                    continue
                results.append({
                    "post_id": str(t.id),
                    "raw_text": text,
                    "post_url": f"https://twitter.com/{handle}/status/{t.id}",
                    "posted_at": _parse_twitter_date(t.created_at),
                })
            logger.debug(f"Twitter @{handle}: {len(results)} pick posts found")
            return results
        except Exception as exc:
            logger.warning(f"Twitter @{handle} fetch failed: {exc}")
            return []

    async def scrape_influencer(self, influencer: dict) -> int:
        """Scrape one influencer and upsert picks into DB. Returns count saved."""
        handle = influencer["handle"]
        influencer_id = influencer["id"]
        db = get_db()

        posts = await self.fetch_user_tweets(handle)
        saved = 0
        for post in posts:
            pick_data = extract_pick(post["raw_text"])
            record = {
                "influencer_id": influencer_id,
                "platform": "twitter",
                "post_id": post["post_id"],
                "post_url": post["post_url"],
                "raw_text": post["raw_text"],
                "predicted_winner": pick_data.get("predicted_winner"),
                "predicted_score": pick_data.get("predicted_score"),
                "confidence": pick_data.get("confidence"),
                "posted_at": post["posted_at"],
            }
            try:
                db.table("picks").upsert(
                    record, on_conflict="platform,post_id"
                ).execute()
                saved += 1
            except Exception as exc:
                logger.warning(f"Failed to save pick from @{handle}: {exc}")

        # Update last_scraped_at
        db.table("influencers").update(
            {"last_scraped_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", influencer_id).execute()

        return saved

    async def scrape_all(self) -> int:
        db = get_db()
        influencers = (
            db.table("influencers")
            .select("id, handle")
            .eq("platform", "twitter")
            .eq("is_active", True)
            .execute()
            .data or []
        )
        logger.info(f"Twitter: scraping {len(influencers)} influencers")
        total = 0
        for inf in influencers:
            count = await self.scrape_influencer(inf)
            total += count
            # polite delay to avoid rate limiting
            await asyncio.sleep(2)
        logger.info(f"Twitter: saved {total} new picks")
        return total


# ─── Seed top sports-pick accounts ──────────────────────────────────────────

TOP_TWITTER_SPORTS_ACCOUNTS = [
    "PickDawgz", "SportsLine", "ActionNetwork", "TheRealKap",
    "BleacherReport", "ESPNBet", "BettingPros", "SharpSide",
    "VegasInsider", "OddsShark", "CleatStreet", "DKSportsbook",
    "FanDuel", "PointsBetUSA", "BetMGM", "Caesars_Sportsbook",
    "WillHill_US", "BetRivers", "Unibet_US", "SuperDraft",
    "PrizePicks", "Underdog_Fantasy", "DraftKings", "FanduelRacing",
    "ProFootballFocus", "AirYards", "FantasyLabs", "RotoWorld",
    "FantasyPros", "4castFball",
    # World Cup specific
    "FIFAWorldCup", "OptaJoe", "StatsBomb", "FBref",
    "WorldFootball_R", "SoccerStatsGuru", "WhoScored",
    "Squawka", "FiveThirtyEight", "ESPN_FC", "BBCSport",
    "SkySports", "Goal", "FourFourTwo", "SoccerAM",
    "ESPNFC", "BeINSPORTS_EN", "Fox_Soccer", "NBCSportsSoccer",
]


async def seed_twitter_influencers() -> int:
    """Add curated list of sports-pick Twitter accounts to the DB."""
    db = get_db()
    records = [
        {"platform": "twitter", "handle": h, "is_active": True}
        for h in TOP_TWITTER_SPORTS_ACCOUNTS
    ]
    result = (
        db.table("influencers")
        .upsert(records, on_conflict="platform,handle", ignore_duplicates=True)
        .execute()
    )
    n = len(result.data or [])
    logger.info(f"Seeded {n} Twitter influencer accounts")
    return n


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _is_pick_post(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in PICK_KEYWORDS)


def _parse_twitter_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S +0000 %Y")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return date_str
