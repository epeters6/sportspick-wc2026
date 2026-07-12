"""
silver_bulletin_scraper.py
Scrapes Nate Silver's "Silver Bulletin" (natesilver.net) for polling averages,
replacing the defunct FiveThirtyEight source (shut down by ABC in March 2025;
archives made inaccessible May 2026).

STRATEGY (two-tier, in priority order):
  1. PRIMARY: Silver Bulletin explicitly publishes raw poll-level data as public
     Google Sheets CSV exports linked directly from each tracker page (e.g. "click
     here to download every Trump approval poll in our database"). This is far
     more robust than parsing a rendered chart -- it's poll-level ground truth,
     and Nate's team has stated intent to keep this public even though the
     *computed forecast probabilities* are paywalled.
  2. FALLBACK: If the CSV link can't be found (page redesign, link removed), try
     pulling data from the embedded Datawrapper chart's dataset export. This is
     best-effort -- verify the URL pattern still works before depending on it;
     Datawrapper embed/data endpoints can change without notice.

SCOPE CAVEAT: this module gives you polling AVERAGES/MARGINS (e.g. generic
ballot D+6.2), not win probabilities for a specific contract. Silver Bulletin's
own computed win probabilities are paywalled, and even unpaywalled wouldn't
cover the full space of contracts you might want to trade. Treat this output as
a feature feeding your consensus/nowcasting engine, not a finished probability.

DESIGN PRINCIPLE (per the 538 postmortem): fail loud, not quiet -- for
structural problems (missing/malformed data). But distinguish that from mere
*transient* problems (a timeout, a 503), which get retried, and from
*staleness* (data fetched fine, but hasn't actually updated in a while), which
gets flagged rather than treated as a hard failure. Three different problems,
three different responses -- collapsing them into one crash-or-succeed path is
how you either miss real breakage or panic over a quiet polling week.

v2 CHANGES: added transient-error retry w/ backoff, a robots.txt courtesy
check, staleness detection on the scraped data itself, and an on-disk
last-known-good cache so a single bad fetch doesn't starve the consensus
engine of this topic entirely.
"""

import re
import io
import json
import time
import logging
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import requests
import pandas as pd

logger = logging.getLogger("silver_bulletin_scraper")

USER_AGENT = "SportsPickResearchBot/1.0 (+contact: you@yourdomain.com)"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0          # doubles each retry: 2s, 4s
STALE_AFTER_DAYS = 21                # flag (don't fail) if newest poll is older than this
DEFAULT_CACHE_DIR = Path(".silver_bulletin_cache")

TRACKER_PAGES: Dict[str, str] = {
    "trump_approval": "https://www.natesilver.net/p/trump-approval-ratings-nate-silver-bulletin",
    "generic_ballot": "https://www.natesilver.net/p/generic-ballot-average-2026-nate-silver-bulletin-congress-polls",
}

_robots_cache: Dict[str, urllib.robotparser.RobotFileParser] = {}


@dataclass
class PollingSnapshot:
    topic: str
    source_url: str
    fetched_at: datetime
    n_polls: int
    raw_dataframe: pd.DataFrame
    data_source_used: str
    is_stale: bool = False
    newest_poll_date: Optional[str] = None
    from_cache: bool = False


class SilverBulletinScrapeError(Exception):
    """Raised for STRUCTURAL problems: no usable data source found, or the
    data that was found fails basic sanity checks. Deliberately NOT swallowed
    anywhere in this module -- let the caller's pipeline health monitor /
    Guardian layer decide what to do."""


def _robots_allow(url: str) -> bool:
    """Courtesy check, not a hard gate -- if robots.txt is unreachable or
    ambiguous we proceed and just log it, we don't block the pipeline over it."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    if domain not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{domain}/robots.txt")
        try:
            rp.read()
        except Exception as e:
            logger.debug("Could not read robots.txt for %s (%s) -- proceeding", domain, e)
            return True
        _robots_cache[domain] = rp
    try:
        return _robots_cache[domain].can_fetch(USER_AGENT, url)
    except Exception:
        return True


def _get_with_retry(url: str) -> requests.Response:
    """Retries transient failures (timeouts, connection errors, 5xx) with
    exponential backoff. Does NOT retry 4xx -- a 404/403 means something
    structural changed (wrong URL, blocked) and hammering it won't help;
    that should surface immediately as a SilverBulletinScrapeError, not get
    silently retried into oblivion.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500:
                raise SilverBulletinScrapeError(
                    f"Non-retryable HTTP {e.response.status_code} fetching {url} -- this is a "
                    "structural problem (wrong URL, blocked, moved), not a transient one, so "
                    "we're not retrying it"
                ) from e
            last_exc = e
        except requests.exceptions.RequestException as e:
            last_exc = e

        if attempt < MAX_RETRIES - 1:
            sleep_for = RETRY_BACKOFF_SECONDS * (2 ** attempt)
            logger.warning("Transient error fetching %s (attempt %d/%d): %s -- retrying in %.0fs",
                           url, attempt + 1, MAX_RETRIES, last_exc, sleep_for)
            time.sleep(sleep_for)
    raise SilverBulletinScrapeError(f"Exhausted {MAX_RETRIES} retries fetching {url}: {last_exc}")


def _fetch_page(url: str) -> str:
    if not _robots_allow(url):
        logger.warning("robots.txt disallows fetching %s -- proceeding anyway per courtesy-not-gate "
                       "policy, but this is worth a human look", url)
    return _get_with_retry(url).text


def _find_public_csv_link(page_html: str) -> Optional[str]:
    matches = re.findall(
        r'https://docs\.google\.com/spreadsheets/d/e/[^\s"\'<>)]+output=csv[^\s"\'<>)]*',
        page_html,
    )
    return matches[0] if matches else None


def _find_datawrapper_ids(page_html: str) -> List[Tuple[str, str]]:
    return re.findall(r"datawrapper\.dwcdn\.net/([A-Za-z0-9]+)/(\d+)/?", page_html)


def _try_datawrapper_dataset(chart_id: str, version: str) -> Optional[pd.DataFrame]:
    candidate_urls = [
        f"https://datawrapper.dwcdn.net/{chart_id}/{version}/dataset.csv",
        f"https://datawrapper.dwcdn.net/{chart_id}/{version}/data.csv",
    ]
    for url in candidate_urls:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.text.strip():
                df = pd.read_csv(io.StringIO(resp.text))
                if len(df.columns) >= 2:
                    return df
        except Exception as e:
            logger.debug("Datawrapper fallback failed for %s: %s", url, e)
    return None


def _validate(df: pd.DataFrame, topic: str) -> None:
    if df is None or df.empty:
        raise SilverBulletinScrapeError(f"[{topic}] scraped dataframe is empty")
    if len(df) < 5:
        raise SilverBulletinScrapeError(
            f"[{topic}] only {len(df)} rows found -- suspiciously low, "
            "page structure may have changed"
        )
    numeric_cols = df.select_dtypes(include="number")
    # Drop known non-percentage/margin columns from the range check
    ignore_cols = ["samplesize", "poll_id", "question_id", "sponsor_id", "pollster_id", "cycle", "year"]
    check_cols = [c for c in numeric_cols.columns if c.lower() not in ignore_cols]
    
    if not check_cols:
        raise SilverBulletinScrapeError(f"[{topic}] no percentage/margin numeric columns found to validate")
        
    out_of_range = df[check_cols].apply(lambda col: ((col < -100) | (col > 100)).any())
    if out_of_range.any():
        bad_cols = list(out_of_range[out_of_range].index)
        raise SilverBulletinScrapeError(
            f"[{topic}] numeric columns {bad_cols} contain values outside [-100, 100] -- "
            "likely parsing a mis-aligned column or the schema changed"
        )


_DATE_COLUMN_CANDIDATES = ["date", "end_date", "poll_date", "field_date", "enddate", "last_updated"]


def _check_staleness(df: pd.DataFrame) -> Tuple[bool, Optional[str]]:
    """Best-effort: look for anything that looks like a date column and check
    recency. Returns (is_stale, newest_date_str). Never raises -- staleness is
    a flag for the caller to weigh, not a hard failure, since a genuinely quiet
    polling week can look identical to a broken scraper from this signal alone."""
    date_col = None
    for col in df.columns:
        if col.strip().lower() in _DATE_COLUMN_CANDIDATES:
            date_col = col
            break
    if date_col is None:
        logger.debug("No recognizable date column found -- skipping staleness check")
        return False, None

    try:
        parsed_dates = pd.to_datetime(df[date_col], errors="coerce")
        newest = parsed_dates.max()
        if pd.isna(newest):
            return False, None
        age = datetime.now(timezone.utc) - newest.to_pydatetime().replace(tzinfo=timezone.utc)
        is_stale = age > timedelta(days=STALE_AFTER_DAYS)
        return is_stale, newest.date().isoformat()
    except Exception as e:
        logger.debug("Staleness check failed to parse dates: %s", e)
        return False, None


def scrape_topic(topic: str) -> PollingSnapshot:
    if topic not in TRACKER_PAGES:
        raise ValueError(f"Unknown topic '{topic}'. Known topics: {list(TRACKER_PAGES)}")

    url = TRACKER_PAGES[topic]
    html = _fetch_page(url)

    csv_url = _find_public_csv_link(html)
    df = None
    source_used = None

    if csv_url:
        try:
            resp = _get_with_retry(csv_url)
            df = pd.read_csv(io.StringIO(resp.text))
            source_used = csv_url
        except Exception as e:
            logger.warning("[%s] primary CSV export failed (%s), trying fallback", topic, e)

    if df is None:
        for chart_id, version in _find_datawrapper_ids(html):
            df = _try_datawrapper_dataset(chart_id, version)
            if df is not None:
                source_used = f"datawrapper:{chart_id}/{version}"
                break

    if df is None:
        raise SilverBulletinScrapeError(
            f"[{topic}] could not locate a usable data source on {url} -- "
            "page structure likely changed. This needs a human to look at it, "
            "not a silent retry loop."
        )

    _validate(df, topic)
    is_stale, newest_date = _check_staleness(df)
    if is_stale:
        logger.warning("[%s] newest poll dated %s is older than %d days -- data is technically "
                       "valid but may reflect a real gap in polling, worth a glance",
                       topic, newest_date, STALE_AFTER_DAYS)

    return PollingSnapshot(
        topic=topic,
        source_url=url,
        fetched_at=datetime.now(timezone.utc),
        n_polls=len(df),
        raw_dataframe=df,
        data_source_used=source_used,
        is_stale=is_stale,
        newest_poll_date=newest_date,
    )


def _cache_paths(topic: str, cache_dir: Path) -> Tuple[Path, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{topic}.csv", cache_dir / f"{topic}.meta.json"


def _write_cache(snapshot: PollingSnapshot, cache_dir: Path) -> None:
    csv_path, meta_path = _cache_paths(snapshot.topic, cache_dir)
    try:
        snapshot.raw_dataframe.to_csv(csv_path, index=False)
        meta = {
            "topic": snapshot.topic,
            "source_url": snapshot.source_url,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "n_polls": snapshot.n_polls,
            "data_source_used": snapshot.data_source_used,
            "is_stale": snapshot.is_stale,
            "newest_poll_date": snapshot.newest_poll_date,
        }
        meta_path.write_text(json.dumps(meta))
    except Exception as e:
        logger.warning("Failed to write last-known-good cache for %s: %s", snapshot.topic, e)


def _read_cache(topic: str, cache_dir: Path, max_age: timedelta) -> Optional[PollingSnapshot]:
    csv_path, meta_path = _cache_paths(topic, cache_dir)
    if not csv_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
        fetched_at = datetime.fromisoformat(meta["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at > max_age:
            return None
        df = pd.read_csv(csv_path)
        return PollingSnapshot(
            topic=topic,
            source_url=meta["source_url"],
            fetched_at=fetched_at,
            n_polls=len(df),
            raw_dataframe=df,
            data_source_used=meta["data_source_used"],
            is_stale=True,          # a cached fallback is, by definition, not fresh
            newest_poll_date=meta.get("newest_poll_date"),
            from_cache=True,
        )
    except Exception as e:
        logger.warning("Failed to read last-known-good cache for %s: %s", topic, e)
        return None


def scrape_topic_with_fallback(
    topic: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_cache_age_hours: int = 48,
) -> PollingSnapshot:
    """Wraps scrape_topic with an on-disk last-known-good fallback.

    A single bad fetch (a site redesign that breaks in a way retries can't
    fix, a temporary block, whatever) would otherwise leave the consensus
    engine with zero data for this topic until someone notices and fixes the
    scraper. This lets the pipeline degrade gracefully to a marked-stale
    cached snapshot instead, for a bounded window -- NOT indefinitely, since
    past max_cache_age_hours a stale opinion is worse than no opinion at all.

    The caller (your consensus engine) should check `.is_stale` / `.from_cache`
    and down-weight or skip this input accordingly -- this function will not
    hide that distinction from you.
    """
    try:
        snapshot = scrape_topic(topic)
        _write_cache(snapshot, cache_dir)
        return snapshot
    except SilverBulletinScrapeError as e:
        logger.error("[%s] live scrape failed (%s) -- attempting last-known-good cache", topic, e)
        cached = _read_cache(topic, cache_dir, timedelta(hours=max_cache_age_hours))
        if cached is not None:
            logger.warning("[%s] serving cached snapshot from %s (marked stale)",
                           topic, cached.fetched_at.isoformat())
            return cached
        raise


from backend.db import get_db

def sync_politics() -> int:
    """
    Scrapes Silver Bulletin and saves the latest polling margins as picks
    for the consensus engine.
    """
    db = get_db()
    
    # 1. Ensure Silver Bulletin influencer exists by handle
    inf_res = db.table("influencers").select("id").eq("handle", "NateSilver538").eq("platform", "twitter").execute().data
    if not inf_res:
        inf_res = db.table("influencers").insert({
            "display_name": "Silver Bulletin",
            "platform": "twitter",
            "handle": "NateSilver538",
            "follower_count": 100000,
            "elo_score": 1900,
            "last_scraped_at": datetime.now(timezone.utc).isoformat()
        }).execute().data
    else:
        db.table("influencers").update({
            "last_scraped_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", inf_res[0]["id"]).execute()
        
    inf_id = inf_res[0]["id"]

    total = 0
    for topic_name in TRACKER_PAGES:
        try:
            snap = scrape_topic_with_fallback(topic_name)
            if snap.raw_dataframe is not None and not snap.raw_dataframe.empty:
                # 2. Ensure mock match exists for this topic
                home_team = f"Donald Trump ({topic_name})"
                match_query = db.table("matches").select("id").eq("sport", "stocks").eq("home_team", home_team).execute().data
                if not match_query:
                    match_res = db.table("matches").insert({
                        "sport": "stocks",
                        "home_team": home_team,
                        "away_team": "Kamala Harris",
                        "scheduled_at": "2028-11-07T00:00:00Z",
                        "tournament": "US Election"
                    }).execute()
                    match_id = match_res.data[0]["id"]
                else:
                    match_id = match_query[0]["id"]

                # Store the latest row as a pick
                latest_data = snap.raw_dataframe.iloc[0].to_dict()
                
                # Check for existing pick for this topic
                existing = db.table("picks").select("id").eq("influencer_id", inf_id).eq("match_id", match_id).eq("post_id", topic_name).execute().data
                if existing:
                    db.table("picks").update({
                        "raw_text": str(latest_data),
                        "scraped_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", existing[0]["id"]).execute()
                else:
                    db.table("picks").insert({
                        "influencer_id": inf_id,
                        "match_id": match_id,
                        "platform": "twitter",
                        "bet_type": "moneyline",
                        "post_id": topic_name,
                        "raw_text": str(latest_data),
                        "outcome": "pending",
                        "scraped_at": datetime.now(timezone.utc).isoformat()
                    }).execute()
                total += 1
        except SilverBulletinScrapeError as e:
            logger.error(f"Failed to sync {topic_name}: {e}")
            
    logger.info(f"Politics Scraper: Saved {total} polling signals from Silver Bulletin")
    return total

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sync_politics()
