"""
YouTube Data API v3 scraper.

Strategy:
1. Re-scrape channels we already track (established pickers, free via uploads playlist).
2. Run keyword searches for new videos — but only admit NEW channels that have
   ≥ MIN_SUBSCRIBERS subscribers (avoids one-off random creators flooding the board).
3. Focused queries target established sports-media channels by name.

FREE tier: 10,000 quota units/day.
  - search          costs 100 units/call  → 10 searches max per day
  - channels.list   costs  1 unit/call    → essentially free
  - playlistItems   costs  1 unit/call    → essentially free
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from loguru import logger

from backend.config import get_settings
from backend.db import get_db
from backend.scrapers.pick_extractor import extract_pick

YT_BASE = "https://www.googleapis.com/youtube/v3"

# Minimum subscriber count for a NEW channel to be added as an influencer.
# Existing tracked channels and KNOWN_CHANNEL_HANDLES are always scraped regardless.
MIN_SUBSCRIBERS = 10_000

# Manually vetted channels — scraped every sync, bypass subscriber gate.
# Add the @handle (YouTube handle format) — resolved to channel IDs automatically.
KNOWN_CHANNEL_HANDLES = [
    "@TheActionNetwork",       # The Action Network — covers every WC match
    "@thefull90",              # thefull90 — active WC2026 picks
    "@Dimers",                 # Dimers Sports Betting Analytics
    "@DocsSports",             # Doc's Sports — in business since 1971, 74k subs
    "@BettingPros",            # BettingPros
    "@Thogden",                # Thogden — large soccer audience
    "@GameDayWagers",          # Chris Vasile (Covers.com expert's own channel)
    "@WagerTalkTV",            # WagerTalk TV — already tracked but ensure it's here
    "@CBSSports",              # CBS Sports — already tracked
    "@PickDawgz",              # PickDawgz — already tracked
]

# Search queries — each costs 100 quota units. Keep to ≤6.
SEARCH_QUERIES = [
    "World Cup 2026 match prediction picks today",
    "World Cup 2026 betting preview expert analysis",
    "FIFA World Cup 2026 group stage picks best bets",
]

MAX_RESULTS_PER_QUERY = 20


class YouTubeScraper:
    def __init__(self):
        self.settings = get_settings()
        self._influencer_cache: dict[str, str] = {}  # channel_id → influencer DB id
        self._sub_count_cache: dict[str, int] = {}   # channel_id → subscriber count

    def _is_configured(self) -> bool:
        return bool(self.settings.youtube_api_key)

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _get_tracked_channels(self) -> list[dict]:
        """Return all active YouTube influencers already in the DB."""
        db = get_db()
        return (
            db.table("influencers")
            .select("id, handle, display_name")
            .eq("platform", "youtube")
            .eq("is_active", True)
            .execute()
            .data or []
        )

    async def _get_or_create_channel(
        self,
        channel_id: str,
        channel_title: str,
        *,
        require_min_subs: bool = True,
        sub_count: int | None = None,
    ) -> str | None:
        if channel_id in self._influencer_cache:
            return self._influencer_cache[channel_id]

        db = get_db()
        existing = (
            db.table("influencers")
            .select("id")
            .eq("platform", "youtube")
            .eq("handle", channel_id)
            .execute()
            .data
        )
        if existing:
            self._influencer_cache[channel_id] = existing[0]["id"]
            return existing[0]["id"]

        # New channel — check subscriber gate
        if require_min_subs:
            subs = sub_count if sub_count is not None else self._sub_count_cache.get(channel_id, -1)
            if subs < MIN_SUBSCRIBERS:
                logger.debug(
                    f"Skipping new channel {channel_title!r} — {subs:,} subscribers < {MIN_SUBSCRIBERS:,}"
                )
                return None

        created = (
            db.table("influencers")
            .insert({
                "platform": "youtube",
                "handle": channel_id,
                "display_name": channel_title,
                "profile_url": f"https://www.youtube.com/channel/{channel_id}",
                "follower_count": sub_count or 0,
                "is_active": True,
            })
            .execute()
            .data
        )
        if created:
            logger.info(f"Added new YouTube channel: {channel_title!r} ({subs:,} subs)")
            self._influencer_cache[channel_id] = created[0]["id"]
            return created[0]["id"]
        return None

    # ── Handle → channel ID resolution ───────────────────────────────────────

    async def _resolve_handles(
        self, client: httpx.AsyncClient, handles: list[str]
    ) -> dict[str, tuple[str, str]]:
        """
        Convert YouTube @handles → (channel_id, title).
        Costs 1 unit per handle. Results are cached so re-runs are free.
        Returns {handle: (channel_id, title)}.
        """
        result: dict[str, tuple[str, str]] = {}
        for handle in handles:
            try:
                r = await client.get(
                    f"{YT_BASE}/channels",
                    params={
                        "part": "snippet,statistics",
                        "forHandle": handle.lstrip("@"),
                        "key": self.settings.youtube_api_key,
                    },
                )
                r.raise_for_status()
                items = r.json().get("items", [])
                if items:
                    cid = items[0]["id"]
                    title = items[0]["snippet"]["title"]
                    subs = int(items[0]["statistics"].get("subscriberCount", 0))
                    self._sub_count_cache[cid] = subs
                    result[handle] = (cid, title)
                await asyncio.sleep(0.2)
            except Exception as exc:
                logger.warning(f"Could not resolve YouTube handle {handle}: {exc}")
        return result

    # ── Quota-cheap channel re-scrape ─────────────────────────────────────────

    async def _scrape_channel_uploads(
        self, client: httpx.AsyncClient, channel_id: str, influencer_id: str
    ) -> list[dict]:
        """
        Fetch recent uploads from a tracked channel via its uploads playlist.
        Cost: 1 unit for channels.list + 1 unit per playlistItems page.
        """
        # Step 1: get uploads playlist ID
        try:
            r = await client.get(
                f"{YT_BASE}/channels",
                params={
                    "part": "contentDetails",
                    "id": channel_id,
                    "key": self.settings.youtube_api_key,
                },
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            if not items:
                return []
            uploads_playlist = (
                items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            )
        except Exception as exc:
            logger.warning(f"Could not get uploads playlist for {channel_id}: {exc}")
            return []

        # Step 2: fetch most-recent 10 videos from uploads playlist (1 unit)
        try:
            r = await client.get(
                f"{YT_BASE}/playlistItems",
                params={
                    "part": "snippet",
                    "playlistId": uploads_playlist,
                    "maxResults": 10,
                    "key": self.settings.youtube_api_key,
                },
            )
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as exc:
            logger.warning(f"Could not fetch uploads for {channel_id}: {exc}")
            return []

        picks = []
        for item in items:
            snippet = item.get("snippet", {})
            vid_id = snippet.get("resourceId", {}).get("videoId")
            if not vid_id:
                continue
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            published_at = snippet.get("publishedAt")
            raw_text = f"{title}\n{description}".strip()

            pick_data = extract_pick(raw_text)
            if not pick_data.get("predicted_winner"):
                continue

            picks.append({
                "influencer_id": influencer_id,
                "vid_id": vid_id,
                "raw_text": raw_text,
                "pick_data": pick_data,
                "published_at": published_at,
            })
        return picks

    # ── Subscriber count lookup ───────────────────────────────────────────────

    async def _fetch_subscriber_counts(
        self, client: httpx.AsyncClient, channel_ids: list[str]
    ) -> dict[str, int]:
        """Batch-fetch subscriber counts for up to 50 channels. Cost: 1 unit per 50."""
        result: dict[str, int] = {}
        for i in range(0, len(channel_ids), 50):
            batch = channel_ids[i : i + 50]
            try:
                r = await client.get(
                    f"{YT_BASE}/channels",
                    params={
                        "part": "statistics",
                        "id": ",".join(batch),
                        "key": self.settings.youtube_api_key,
                    },
                )
                r.raise_for_status()
                for item in r.json().get("items", []):
                    cid = item["id"]
                    subs = int(item["statistics"].get("subscriberCount", 0))
                    result[cid] = subs
                    self._sub_count_cache[cid] = subs
            except Exception as exc:
                logger.warning(f"Subscriber count batch failed: {exc}")
        return result

    # ── Save pick to DB ───────────────────────────────────────────────────────

    def _save_pick(self, db, influencer_id: str, vid_id: str, raw_text: str,
                   pick_data: dict, published_at: str | None) -> bool:
        record = {
            "influencer_id": influencer_id,
            "platform": "youtube",
            "post_id": vid_id,
            "post_url": f"https://www.youtube.com/watch?v={vid_id}",
            "raw_text": raw_text[:2000],
            "predicted_winner": pick_data.get("predicted_winner"),
            "predicted_score": pick_data.get("predicted_score"),
            "confidence": pick_data.get("confidence"),
            "posted_at": published_at,
        }
        try:
            db.table("picks").upsert(record, on_conflict="platform,post_id").execute()
            return True
        except Exception as exc:
            logger.warning(f"Failed to save YouTube pick: {exc}")
            return False

    # ── Main entry point ──────────────────────────────────────────────────────

    async def scrape_all(self) -> int:
        if not self._is_configured():
            logger.warning("YouTube scraper skipped — YOUTUBE_API_KEY not set")
            return 0

        db = get_db()
        total = 0
        seen_video_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=20) as client:

            # ── Phase 0: Resolve & seed known vetted channels ─────────────────
            resolved = await self._resolve_handles(client, KNOWN_CHANNEL_HANDLES)
            for handle, (channel_id, channel_title) in resolved.items():
                subs = self._sub_count_cache.get(channel_id, 0)
                # Known channels bypass the subscriber gate
                await self._get_or_create_channel(
                    channel_id, channel_title,
                    require_min_subs=False,
                    sub_count=subs,
                )

            # ── Phase 1: Re-scrape established channels (cheap, 2 units/channel) ──
            tracked = await self._get_tracked_channels()
            if tracked:
                logger.info(f"YouTube: re-scraping {len(tracked)} tracked channels")
                for channel in tracked:
                    channel_id = channel["handle"]
                    influencer_id = channel["id"]
                    self._influencer_cache[channel_id] = influencer_id

                    channel_picks = await self._scrape_channel_uploads(
                        client, channel_id, influencer_id
                    )
                    for cp in channel_picks:
                        vid_id = cp["vid_id"]
                        if vid_id in seen_video_ids:
                            continue
                        seen_video_ids.add(vid_id)
                        if self._save_pick(
                            db, influencer_id, vid_id,
                            cp["raw_text"], cp["pick_data"], cp["published_at"]
                        ):
                            total += 1
                    await asyncio.sleep(0.5)

            # ── Phase 2: Keyword search for new channels ──────────────────────
            new_channel_ids: list[str] = []
            search_videos: list[dict] = []

            for query in SEARCH_QUERIES:
                try:
                    videos = await self._search_videos(client, query)
                    logger.info(f"YouTube '{query}': {len(videos)} videos")
                    for video in videos:
                        vid_id = video.get("id", {}).get("videoId")
                        if not vid_id or vid_id in seen_video_ids:
                            continue
                        snippet = video.get("snippet", {})
                        channel_id = snippet.get("channelId", "")
                        # Collect new channels to batch-check subscriber counts
                        if channel_id and channel_id not in self._influencer_cache:
                            new_channel_ids.append(channel_id)
                        search_videos.append(video)
                    await asyncio.sleep(1)
                except Exception as exc:
                    logger.warning(f"YouTube query '{query}' failed: {exc}")

            # Batch-fetch subscriber counts for all new channels found in search
            if new_channel_ids:
                unique_new = list(dict.fromkeys(new_channel_ids))  # deduplicate, preserve order
                await self._fetch_subscriber_counts(client, unique_new)

            # Process search results
            for video in search_videos:
                vid_id = video.get("id", {}).get("videoId")
                if not vid_id or vid_id in seen_video_ids:
                    continue
                seen_video_ids.add(vid_id)

                snippet = video.get("snippet", {})
                channel_id = snippet.get("channelId", "")
                channel_title = snippet.get("channelTitle", "Unknown")
                title = snippet.get("title", "")
                description = snippet.get("description", "")
                published_at = snippet.get("publishedAt")
                raw_text = f"{title}\n{description}".strip()

                pick_data = extract_pick(raw_text)
                if not pick_data.get("predicted_winner"):
                    continue

                sub_count = self._sub_count_cache.get(channel_id, 0)
                influencer_id = await self._get_or_create_channel(
                    channel_id, channel_title,
                    require_min_subs=True,
                    sub_count=sub_count,
                )
                if not influencer_id:
                    continue

                if self._save_pick(db, influencer_id, vid_id, raw_text, pick_data, published_at):
                    total += 1

        logger.info(f"YouTube: saved {total} picks")
        return total

    async def _search_videos(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": MAX_RESULTS_PER_QUERY,
            "order": "date",
            "relevanceLanguage": "en",
            "key": self.settings.youtube_api_key,
        }
        r = await client.get(f"{YT_BASE}/search", params=params)
        r.raise_for_status()
        return r.json().get("items", [])
