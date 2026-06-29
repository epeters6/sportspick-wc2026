"""
Twitter / X scraper using twikit (cookie-based, free, unofficial).

twikit uses your own account's cookies so it does NOT require an API key.
Set TWITTER_AUTH_TOKEN and TWITTER_CT0 in .env — grab them from browser
DevTools → Application → Cookies → twitter.com after logging in.

Discovery mirrors YouTube: keyword search finds new pick-posting accounts, but
only accounts with >= MIN_FOLLOWERS are added (loose gate — smaller cappers OK).
Manually vetted KNOWN_TWITTER_HANDLES bypass the follower gate.
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
from backend.scrapers.pick_extractor import extract_all_picks

settings = get_settings()

# Loose follower gate for newly discovered accounts (YouTube uses 10k).
MIN_FOLLOWERS = 500

# Sports-pick related keywords used to filter tweets
PICK_KEYWORDS = [
    "prediction", "predict", "pick", "picks", "winner", "final score",
    "bet", "wager", "odds", "lock", "locks", "parlay", "moneyline", "ml ",
    "💰", "🏆", "#worldcup", "#wc2026", "#mlb", "#gamblingtwitter", "#gamblingx",
    "going to win", "will win", "beat", "over", "under", "best bet",
    "free pick", "potd", "play of the day",
]

# Max tweets to fetch per influencer per scrape cycle
TWEETS_PER_USER = 20

# Keyword searches for discovering new pick-posting accounts (keep small — rate limits)
TWITTER_SEARCH_QUERIES = [
    "MLB picks today",
    "World Cup 2026 prediction pick",
    "sports betting pick moneyline",
]

# Manually vetted — always scraped and bypass follower gate on discovery
KNOWN_TWITTER_HANDLES = {
    # User-requested cappers
    "locked_lines", "linelockbets", "parlayscience", "parlayclubplays",
    "theparlayhawk0", "sharplinesports", "sharppickss", "warrensharp",
    # Top betting / picks accounts
    "pickdawgz", "sportsline", "actionnetwork", "bettingpros", "vsin",
    "wagertalk", "picksnparlays", "picksparlays", "fezzikpicks",
    "therocketplays", "fourfigureplays", "clintledlocks", "locksOverunder",
    "lightninglockz", "theparlayhawk0", "parlay_pros_",
    # Books / media with regular picks content
    "espnbet", "dkSportsbook", "covers", "oddsshark", "vegasinsider",
    # Soccer / WC analysts
    "optajoe", "statsbomb", "fbref", "espn_fc", "bbcsport", "skysports",
    "goal", "fifaworldcup", "fox_soccer", "nbcsportssoccer",
    # MLB-focused
    "mlb", "mlbtraderumors", "fantasypros", "rotoworld",
}

# Seed list (union of known + additional accounts to track)
TOP_TWITTER_SPORTS_ACCOUNTS = sorted({
    # ── User-requested ────────────────────────────────────────────────────────
    "Locked_Lines", "LineLockBets", "ParlayScience", "ParlayClubPlays",
    "theparlayhawk0", "SharpLineSports", "sharppickss", "WarrenSharp",
    # ── Major cappers / pick services ─────────────────────────────────────────
    "PickDawgz", "SportsLine", "ActionNetwork", "BettingPros", "VSiN",
    "WagerTalk", "PicksParlays", "FezzikPicks", "therocketplays",
    "FourFigurePlays", "ClintledLocks", "LocksOverUnder", "LightningLockz",
    "Parlay_Pros_", "TheRealKap", "SharpSide", "CleatStreet",
    # ── Sportsbooks / media ───────────────────────────────────────────────────
    "ESPNBet", "DKSportsbook", "FanDuel", "BetMGM", "Caesars_Sportsbook",
    "OddsShark", "VegasInsider", "Covers", "BleacherReport",
    # ── Soccer / WC ───────────────────────────────────────────────────────────
    "FIFAWorldCup", "OptaJoe", "StatsBomb", "FBref", "ESPN_FC", "BBCSport",
    "SkySports", "Goal", "FourFourTwo", "Fox_Soccer", "NBCSportsSoccer",
    "Squawka", "WhoScored", "BeINSPORTS_EN",
    # ── MLB / multi-sport ─────────────────────────────────────────────────────
    "MLB", "FantasyPros", "RotoWorld", "ProFootballFocus",
})


class TwitterScraper:
    def __init__(self):
        if TwikitClient is None:
            raise RuntimeError("twikit is not installed — run: pip install twikit")
        try:
            self.client = TwikitClient(language="en-US", impersonate="chrome124")
        except TypeError:
            self.client = TwikitClient(language="en-US")
        self._authenticated = False
        self._known_handles = {h.lower() for h in KNOWN_TWITTER_HANDLES}
        self._known_handles.update(h.lower() for h in TOP_TWITTER_SPORTS_ACCOUNTS)

    async def authenticate(self) -> None:
        if self._authenticated:
            return
        if not settings.twitter_auth_token or not settings.twitter_ct0:
            logger.warning("Twitter cookies not configured — scraper disabled")
            return
        try:
            self.client.set_cookies({
                "auth_token": settings.twitter_auth_token,
                "ct0": settings.twitter_ct0,
            })
            self._authenticated = True
            logger.info("Twitter: authenticated via cookies")
        except Exception as exc:
            logger.error(f"Twitter auth failed: {exc}")

    def _passes_follower_gate(self, handle: str, followers: int) -> bool:
        if handle.lower() in self._known_handles:
            return True
        if followers >= MIN_FOLLOWERS:
            return True
        logger.debug(
            f"Twitter @{handle}: {followers:,} followers < {MIN_FOLLOWERS:,} — skipped"
        )
        return False

    async def _upsert_influencer(
        self, handle: str, *, followers: int = 0, display_name: str | None = None,
    ) -> str | None:
        """Add or update a Twitter influencer if it passes the follower gate."""
        handle = handle.lstrip("@")
        if not self._passes_follower_gate(handle, followers):
            return None
        db = get_db()
        row = (
            db.table("influencers")
            .upsert(
                {
                    "platform": "twitter",
                    "handle": handle,
                    "display_name": display_name or handle,
                    "follower_count": followers,
                    "profile_url": f"https://twitter.com/{handle}",
                    "is_active": True,
                },
                on_conflict="platform,handle",
            )
            .execute()
            .data
        )
        return row[0]["id"] if row else None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
    async def fetch_user_tweets(self, handle: str) -> list[dict]:
        await self.authenticate()
        if not self._authenticated:
            return []
        try:
            user = await self.client.get_user_by_screen_name(handle.lstrip("@"))
            followers = getattr(user, "followers_count", 0) or 0
            await self._upsert_influencer(
                handle, followers=followers,
                display_name=getattr(user, "name", None),
            )

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

    async def _discover_accounts(self) -> int:
        """Search X for pick posts and admit new accounts that pass follower gate."""
        await self.authenticate()
        if not self._authenticated:
            return 0

        db = get_db()
        existing = {
            r["handle"].lower()
            for r in (
                db.table("influencers")
                .select("handle")
                .eq("platform", "twitter")
                .execute()
                .data or []
            )
        }
        added = 0
        seen: set[str] = set()

        for query in TWITTER_SEARCH_QUERIES:
            try:
                result = await self.client.search_tweet(query, "Latest", count=30)
                tweets = list(result) if result else []
                for t in tweets:
                    user = getattr(t, "user", None)
                    if not user:
                        continue
                    handle = (getattr(user, "screen_name", None) or "").strip()
                    if not handle or handle.lower() in seen or handle.lower() in existing:
                        continue
                    text = t.full_text or t.text or ""
                    if not _is_pick_post(text):
                        continue
                    seen.add(handle.lower())
                    followers = getattr(user, "followers_count", 0) or 0
                    iid = await self._upsert_influencer(
                        handle, followers=followers,
                        display_name=getattr(user, "name", None),
                    )
                    if iid:
                        existing.add(handle.lower())
                        added += 1
                await asyncio.sleep(3)
            except Exception as exc:
                logger.warning(f"Twitter search '{query}' failed: {exc}")

        if added:
            logger.info(f"Twitter: discovered {added} new accounts from search")
        return added

    async def scrape_influencer(self, influencer: dict) -> int:
        """Scrape one influencer and upsert picks into DB. Returns count saved."""
        handle = influencer["handle"]
        influencer_id = influencer["id"]
        db = get_db()

        posts = await self.fetch_user_tweets(handle)
        saved = 0
        for post in posts:
            all_picks = extract_all_picks(post["raw_text"])
            if not all_picks:
                continue
            for i, pick_data in enumerate(all_picks):
                post_id = (
                    f"{post['post_id']}-pick-{i}"
                    if len(all_picks) > 1
                    else post["post_id"]
                )
                record = {
                    "influencer_id": influencer_id,
                    "platform": "twitter",
                    "post_id": post_id,
                    "post_url": post["post_url"],
                    "raw_text": post["raw_text"],
                    "predicted_winner": pick_data.get("predicted_winner"),
                    "predicted_score": pick_data.get("predicted_score"),
                    "confidence": pick_data.get("confidence"),
                    "bet_type": pick_data.get("bet_type"),
                    "bet_line": pick_data.get("bet_line"),
                    "bet_subject": pick_data.get("bet_subject"),
                    "posted_at": post["posted_at"],
                }
                try:
                    db.table("picks").upsert(
                        record, on_conflict="platform,post_id"
                    ).execute()
                    saved += 1
                except Exception as exc:
                    logger.warning(f"Failed to save pick from @{handle}: {exc}")

        db.table("influencers").update(
            {"last_scraped_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", influencer_id).execute()

        return saved

    async def scrape_all(self) -> int:
        db = get_db()
        await self._discover_accounts()

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
            await asyncio.sleep(2)
        logger.info(f"Twitter: saved {total} new picks")
        return total


async def seed_twitter_influencers() -> int:
    """Add curated list of sports-pick Twitter accounts to the DB."""
    db = get_db()
    records = [
        {
            "platform": "twitter",
            "handle": h,
            "is_active": True,
            "profile_url": f"https://twitter.com/{h}",
        }
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
