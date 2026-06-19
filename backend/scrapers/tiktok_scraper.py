"""
TikTok scraper using the unofficial TikTok-Api Python library (davidteather).

Requires Playwright browser under the hood. Install with:
    pip install tiktok-api
    playwright install chromium

Session cookie (TIKTOK_SESSION_ID) makes it more stable — grab from browser
DevTools → Application → Cookies → tiktok.com after logging in.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    from TikTokApi import TikTokApi
    from TikTokApi.exceptions import TikTokException
except Exception as _tt_err:
    import sys
    print(f"[TikTokApi import failed] {_tt_err}", file=sys.stderr)
    TikTokApi = None  # type: ignore
    TikTokException = Exception

from backend.config import get_settings
from backend.db import get_db
from backend.scrapers.pick_extractor import extract_pick

settings = get_settings()

VIDEOS_PER_USER = 15

PICK_KEYWORDS = [
    "prediction", "predict", "pick", "winner", "bet",
    "wager", "🏆", "💰", "#worldcup", "#wc2026", "going to win",
    "will win", "final score", "my take", "calling it",
]

TOP_TIKTOK_SPORTS_ACCOUNTS = [
    "worldcuppredictions", "soccerpicks2026", "footballpredictions",
    "wc2026picks", "sportspredictions", "thesportsguy",
    "bettingtips", "soccertips", "worldcupbets",
    "footballanalysis", "socceranalyst", "the_pick_daddy",
    "cappertek", "sharpaction", "pickemking",
    "freepicks_daily", "winnerpicks", "topbettingtips",
    "lockedinpicks", "godpicks",
]


class TikTokScraper:
    def __init__(self):
        if TikTokApi is None:
            raise RuntimeError("TikTokApi is not installed — run: pip install tiktok-api")
        self.api: TikTokApi | None = None

    async def _get_api(self) -> TikTokApi:
        if self.api is None:
            self.api = TikTokApi()
            ms_token = settings.tiktok_session_id or None
            ms_token = settings.tiktok_ms_token or settings.tiktok_session_id or None
            await self.api.create_sessions(
                num_sessions=1,
                headless=False,
                browser="webkit",
                sleep_after=5,
                ms_tokens=[ms_token] if ms_token else None,
            )
            logger.info("TikTok API session created")
        return self.api

    async def close(self):
        if self.api:
            try:
                await self.api.close_sessions()
            except Exception:
                pass
            self.api = None

    async def fetch_user_videos(self, handle: str) -> list[dict]:
        api = await self._get_api()
        results = []
        try:
            user = api.user(username=handle)
            async for video in user.videos(count=VIDEOS_PER_USER):
                desc = video.as_dict.get("desc", "") or ""
                if not _is_pick_post(desc):
                    continue
                vid_id = video.id
                results.append({
                    "post_id": str(vid_id),
                    "raw_text": desc,
                    "post_url": f"https://www.tiktok.com/@{handle}/video/{vid_id}",
                    "posted_at": _ts_to_iso(video.as_dict.get("createTime")),
                })
            logger.debug(f"TikTok @{handle}: {len(results)} pick videos")
        except Exception as exc:
            logger.warning(f"TikTok @{handle} failed: {exc}")
        return results

    async def scrape_influencer(self, influencer: dict) -> int:
        handle = influencer["handle"]
        influencer_id = influencer["id"]
        db = get_db()

        posts = await self.fetch_user_videos(handle)
        saved = 0
        for post in posts:
            pick_data = extract_pick(post["raw_text"])
            record = {
                "influencer_id": influencer_id,
                "platform": "tiktok",
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
                logger.warning(f"Failed to save TikTok pick from @{handle}: {exc}")

        db.table("influencers").update(
            {"last_scraped_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", influencer_id).execute()
        return saved

    async def scrape_all(self) -> int:
        db = get_db()
        influencers = (
            db.table("influencers")
            .select("id, handle")
            .eq("platform", "tiktok")
            .eq("is_active", True)
            .execute()
            .data or []
        )
        logger.info(f"TikTok: scraping {len(influencers)} influencers")

        # Run in a thread with its own ProactorEventLoop — Playwright needs
        # subprocess support which is unavailable in uvicorn's anyio event loop
        loop = asyncio.get_event_loop()
        total = await loop.run_in_executor(None, _run_tiktok_sync, influencers)
        logger.info(f"TikTok: saved {total} new picks")
        return total


async def seed_tiktok_influencers() -> int:
    db = get_db()
    records = [
        {"platform": "tiktok", "handle": h, "is_active": True}
        for h in TOP_TIKTOK_SPORTS_ACCOUNTS
    ]
    result = (
        db.table("influencers")
        .upsert(records, on_conflict="platform,handle", ignore_duplicates=True)
        .execute()
    )
    n = len(result.data or [])
    logger.info(f"Seeded {n} TikTok accounts")
    return n


def _is_pick_post(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in PICK_KEYWORDS)


def _ts_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _run_tiktok_sync(influencers: list[dict]) -> int:
    """
    Run TikTok scraping in a fresh thread with a ProactorEventLoop.
    Playwright requires subprocess support which only ProactorEventLoop
    provides on Windows.
    """
    import sys

    async def _inner():
        scraper = TikTokScraper()
        total = 0
        try:
            for inf in influencers:
                count = await scraper.scrape_influencer(inf)
                total += count
                await asyncio.sleep(5)
        finally:
            await scraper.close()
        return total

    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_inner())
    except Exception as exc:
        logger.warning(f"TikTok thread runner failed: {exc}")
        return 0
    finally:
        loop.close()
