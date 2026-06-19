"""
Instagram scraper using Instaloader (free, unofficial).

Instaloader downloads public profile posts. It rate-limits aggressively,
so we scrape ~5-10 posts per user and sleep generously between requests.

Set INSTAGRAM_USERNAME + INSTAGRAM_PASSWORD in .env for authenticated sessions
(higher rate limits and access to more posts).
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    import instaloader
    from instaloader import Instaloader, Profile
except ImportError:
    instaloader = None  # type: ignore
    Instaloader = None  # type: ignore

from backend.config import get_settings
from backend.db import get_db
from backend.scrapers.pick_extractor import extract_pick

settings = get_settings()

POSTS_PER_USER = 10

PICK_KEYWORDS = [
    "prediction", "predict", "pick", "winner", "bet", "wager",
    "🏆", "💰", "#worldcup", "#wc2026", "going to win", "will win",
    "final score", "calling it", "worldcuppick",
]

TOP_INSTAGRAM_SPORTS_ACCOUNTS = [
    "goal", "skysports", "sportsbible", "433",
    "footballdaily", "bbcsport", "espnfc",
    "bleacherreport", "draftkings", "fanduel",
    "bet365", "betway", "williamhill",
    "oddschecker", "bettingexpert",
    "soccerway", "transfermarkt",
    "opta", "squawka", "whoscored",
]


class InstagramScraper:
    _loader: Any = None
    _executor = ThreadPoolExecutor(max_workers=1)

    def _get_loader(self) -> Any:
        if instaloader is None:
            raise RuntimeError("instaloader not installed — run: pip install instaloader")
        if self._loader is None:
            self._loader = Instaloader(
                quiet=True,
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                save_metadata=False,
                compress_json=False,
                request_timeout=30,
            )
            session_file = f"ig_session_{settings.instagram_username}"
            import os
            if settings.instagram_username:
                # Try loading a saved session first (avoids login challenge)
                if os.path.exists(session_file):
                    try:
                        self._loader.load_session_from_file(
                            settings.instagram_username, session_file
                        )
                        logger.info("Instagram: loaded saved session")
                        return self._loader
                    except Exception:
                        pass
                # Fall back to password login and save session
                if settings.instagram_password:
                    try:
                        self._loader.login(
                            settings.instagram_username, settings.instagram_password
                        )
                        self._loader.save_session_to_file(session_file)
                        logger.info("Instagram: logged in and saved session")
                    except Exception as exc:
                        logger.warning(f"Instagram login failed: {exc} — using anonymous mode")
        return self._loader

    def _fetch_posts_sync(self, handle: str) -> list[dict]:
        """Runs in thread executor (Instaloader is synchronous)."""
        loader = self._get_loader()
        results = []
        try:
            profile = Profile.from_username(loader.context, handle)
            for i, post in enumerate(profile.get_posts()):
                if i >= POSTS_PER_USER:
                    break
                caption = post.caption or ""
                if not _is_pick_post(caption):
                    continue
                results.append({
                    "post_id": str(post.shortcode),
                    "raw_text": caption,
                    "post_url": f"https://www.instagram.com/p/{post.shortcode}/",
                    "posted_at": post.date_utc.replace(tzinfo=timezone.utc).isoformat(),
                })
        except Exception as exc:
            logger.warning(f"Instagram @{handle} failed: {exc}")
        return results

    async def fetch_user_posts(self, handle: str) -> list[dict]:
        loop = asyncio.get_event_loop()
        posts = await loop.run_in_executor(
            self._executor, self._fetch_posts_sync, handle
        )
        logger.debug(f"Instagram @{handle}: {len(posts)} pick posts")
        return posts

    async def scrape_influencer(self, influencer: dict) -> int:
        handle = influencer["handle"]
        influencer_id = influencer["id"]
        db = get_db()

        posts = await self.fetch_user_posts(handle)
        saved = 0
        for post in posts:
            pick_data = extract_pick(post["raw_text"])
            record = {
                "influencer_id": influencer_id,
                "platform": "instagram",
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
                logger.warning(f"Failed to save IG pick from @{handle}: {exc}")

        db.table("influencers").update(
            {"last_scraped_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", influencer_id).execute()
        return saved

    async def scrape_all(self) -> int:
        db = get_db()
        influencers = (
            db.table("influencers")
            .select("id, handle")
            .eq("platform", "instagram")
            .eq("is_active", True)
            .execute()
            .data or []
        )
        logger.info(f"Instagram: scraping {len(influencers)} influencers")
        total = 0
        for inf in influencers:
            count = await self.scrape_influencer(inf)
            total += count
            await asyncio.sleep(8)  # Instagram is aggressive — be polite
        logger.info(f"Instagram: saved {total} new picks")
        return total


async def seed_instagram_influencers() -> int:
    db = get_db()
    records = [
        {"platform": "instagram", "handle": h, "is_active": True}
        for h in TOP_INSTAGRAM_SPORTS_ACCOUNTS
    ]
    result = (
        db.table("influencers")
        .upsert(records, on_conflict="platform,handle", ignore_duplicates=True)
        .execute()
    )
    n = len(result.data or [])
    logger.info(f"Seeded {n} Instagram accounts")
    return n


def _is_pick_post(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in PICK_KEYWORDS)
