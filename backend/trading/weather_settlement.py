import asyncio
import httpx
import re
from datetime import datetime, timezone
from loguru import logger
from backend.db import get_db
from backend.trading.kalshi_client import KalshiClient
from backend.trading.settlement_integrity import WEATHER_SETTLEMENT_VERSION

import os
import sys

# Add pavlov to path for poly_client (lazy — avoid requiring secrets at import)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../pavlov")))


def _poly_client():
    """Lazy import so unit tests can load settlement helpers without trading secrets."""
    os.environ.setdefault("PAVLOV_BYPASS_CONFIG", "1")
    from polymarket import poly_client as _pc
    return _pc

# Kalshi tickers are typically uppercase: KXHIGHTDC-26JUL06-T84
_KALSHI_TICKER_RE = re.compile(r'^[A-Z]{2,}[A-Z0-9-]+$')
# Pattern to extract date from Kalshi ticker: e.g. 26JUL06 -> 2026-07-06
_KALSHI_DATE_RE = re.compile(r'-(\d{2})([A-Z]{3})(\d{2})-')
_MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}


def _detect_venue(market_id: str) -> str:
    """Auto-detect whether a market_id is Kalshi or Polymarket based on format."""
    if not market_id:
        return "polymarket"
    # Kalshi tickers are ALL-CAPS with dashes (e.g. KXHIGHTDC-26JUL06-T84)
    if _KALSHI_TICKER_RE.match(market_id):
        return "kalshi"
    return "polymarket"


def _kalshi_ticker_date(market_id: str) -> datetime | None:
    """Extract the settlement date from a Kalshi ticker like KXHIGHTDC-26JUL06-T84.
    Returns a timezone-aware datetime (UTC midnight) or None if unparseable."""
    m = _KALSHI_DATE_RE.search(market_id)
    if not m:
        return None
    try:
        yr = 2000 + int(m.group(1))
        mo = _MONTH_MAP.get(m.group(2).upper())
        day = int(m.group(3))
        if mo is None:
            return None
        return datetime(yr, mo, day, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None



def _grade_bet_against_actual(bet: dict, market_date: datetime) -> dict | None:
    """Grade an unresolved weather bet against the observed station temperature.

    Returns (status, pnl) — ('won'|'lost', pnl) — or None if the actual
    temperature can't be determined (METAR archive only reaches back ~4 days).
    """
    meta = bet.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}

    station = meta.get("station")
    metric = meta.get("metric") or "high"
    target_date = meta.get("target_date")
    lo = meta.get("bucket_low_f")
    hi = meta.get("bucket_high_f")

    # Legacy bets (before metadata enrichment): recover from question text
    # "Weather: {city} High {label} {date} ({platform})"
    question = bet.get("question") or ""
    if not target_date:
        m = re.search(r'(\d{4}-\d{2}-\d{2})', question)
        target_date = m.group(1) if m else None
    if station is None or (lo is None and hi is None):
        m = re.match(r'Weather:\s+(.+?)\s+(High|Low)\s+(\S+)\s+\d{4}-\d{2}-\d{2}', question)
        if m:
            city_name, metric_word, label = m.group(1), m.group(2), m.group(3)
            metric = metric_word.lower()
            try:
                from pavlov.pipeline.station_mapper import STATION_MAP
                station = station or (STATION_MAP.get(city_name) or {}).get("station")
            except Exception:
                pass
            # Parse label: ">73", "<70", "71-72", "71" — same half-degree
            # conventions as settlement_resolver.parse_bucket_bounds
            gm = re.match(r'^>(\d+(?:\.\d+)?)$', label)
            lm = re.match(r'^<(\d+(?:\.\d+)?)$', label)
            bm = re.match(r'^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$', label)
            sm = re.match(r'^(\d+(?:\.\d+)?)$', label)
            if gm:
                lo, hi = float(gm.group(1)) - 0.5, None
            elif lm:
                lo, hi = None, float(lm.group(1)) + 0.5
            elif bm:
                lo, hi = float(bm.group(1)) - 0.5, float(bm.group(2)) + 0.5
            elif sm:
                lo, hi = float(sm.group(1)) - 0.5, float(sm.group(1)) + 0.5

    if not station or not target_date or (lo is None and hi is None):
        return None

    try:
        from backend.ml.weather_verification import fetch_actual_extremes
        actual = fetch_actual_extremes(station, str(target_date)[:10])
    except Exception as e:
        logger.warning(f"Actual-temp fetch failed for {station} {target_date}: {e}")
        return None

    observed = actual.get(metric)
    if observed is None:
        return None

    lo_v = float(lo) if lo is not None else float("-inf")
    hi_v = float(hi) if hi is not None else float("inf")
    in_bucket = lo_v <= observed <= hi_v

    backed_yes = str(bet.get("outcome_name") or "yes").lower() == "yes"
    won = in_bucket if backed_yes else not in_bucket

    stake = bet.get("stake") or 0.0
    shares = bet.get("shares") or 0.0
    price = bet.get("market_price") or 0.0
    pnl = round(shares * (1 - price), 2) if won else round(-stake, 2)
    logger.info(
        f"Graded weather bet {bet['id'][:8]} vs actual {metric}={observed}°F "
        f"(bucket [{lo_v}, {hi_v}]) -> {'won' if won else 'lost'}"
    )
    return {
        "status": "won" if won else "lost",
        "pnl": pnl,
        "actual_temp_f": float(observed),
        "metric": metric,
        "station": station,
        "target_date": str(target_date)[:10],
        "bucket_low_f": None if lo_v == float("-inf") else lo_v,
        "bucket_high_f": None if hi_v == float("inf") else hi_v,
        "in_bucket": in_bucket,
    }


def _ensure_poly_configured():
    """Ensure poly_client can make unauthenticated requests for settlement checks."""
    poly_client = _poly_client()
    if not poly_client.poly_configured():
        # Override to allow reading public settlement data without trading keys
        poly_client.poly_configured = lambda: True
        def mock_get_client():
            from polymarket_us import PolymarketUS
            return PolymarketUS(key_id="dummy", secret_key="dummy")
        poly_client.get_client = mock_get_client
    return poly_client


async def check_polymarket_resolution(slug: str) -> dict | None:
    """Check Polymarket US API for market resolution using poly_client."""
    poly_client = _ensure_poly_configured()
    try:
        # get_market_result returns 'yes' or 'no' if settled, else None
        res = poly_client.get_market_result(slug)
        if res:
            return {
                "closed": True,
                "active": False,
                "resolved": True,
                "winner": str(res).lower(),
                "venue": "polymarket",
                "market_id": slug,
            }
    except Exception as e:
        logger.warning(f"Error fetching Polymarket resolution for {slug}: {e}")
    return None


async def check_kalshi_resolution(ticker: str) -> dict | None:
    """Check Kalshi API for market resolution."""
    client = KalshiClient()
    try:
        async with httpx.AsyncClient() as http_client:
            data = await client._get(http_client, f"/markets/{ticker}")
            if data and "market" in data:
                m = data["market"]
                status = str(m.get("status") or "").lower()
                result = str(m.get("result") or "").lower() or None
                return {
                    "closed": status in ("closed", "settled", "determined", "finalized"),
                    "resolved": status in ("settled", "determined", "finalized")
                    and result in ("yes", "no"),
                    "winner": result,
                    "venue": "kalshi",
                    "market_id": ticker,
                    "status": status,
                    "settled_at": m.get("settlement_ts") or m.get("settled_time"),
                    "settlement_value": (
                        m.get("settlement_value_dollars")
                        if m.get("settlement_value_dollars") is not None
                        else m.get("settlement_value")
                    ),
                    "rules_primary": m.get("rules_primary"),
                    "title": m.get("title"),
                }
    except Exception as e:
        logger.warning(f"Error fetching Kalshi resolution for {ticker}: {e}")
    return None


def _bet_meta(bet: dict) -> dict:
    meta = bet.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _target_date_for_bet(bet: dict) -> datetime | None:
    """Return target local settlement date as UTC-midnight datetime when known."""
    meta = _bet_meta(bet)
    target = meta.get("target_date")
    if target:
        try:
            d = datetime.strptime(str(target)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return d
        except ValueError:
            pass
    market_id = bet.get("market_id") or ""
    kd = _kalshi_ticker_date(market_id)
    if kd is not None:
        return kd
    question = bet.get("question") or ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", question)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _city_and_metric_for_bet(bet: dict) -> tuple[str | None, str]:
    """City + metric for readiness checks; recover from question for legacy rows."""
    meta = _bet_meta(bet)
    city = meta.get("city")
    metric = (meta.get("metric") or "").lower()
    if city and metric in ("high", "low"):
        return city, metric

    question = bet.get("question") or ""
    m = re.match(r"Weather:\s+(.+?)\s+(High|Low)\s+", question)
    if m:
        city = city or m.group(1).strip()
        metric = metric or m.group(2).lower()
    if metric not in ("high", "low"):
        metric = "high"
    return city, metric


def _actuals_ready_to_grade(bet: dict, now: datetime) -> bool:
    """True when a complete station day is available for diagnostics.

    A station extreme is not a venue outcome and must never settle a traded
    contract. Waiting until the next local day also prevents partial-day high or
    low observations from entering forecast verification.
    """
    city, _metric = _city_and_metric_for_bet(bet)
    target_dt = _target_date_for_bet(bet)
    if target_dt is None:
        return False
    target_date = target_dt.date()

    try:
        from zoneinfo import ZoneInfo
        from pavlov.pipeline.station_mapper import get_tz_for_city
        if city:
            local_now = now.astimezone(ZoneInfo(get_tz_for_city(city)))
        else:
            # US weather markets default Eastern when city metadata is missing.
            local_now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        local_now = now.astimezone(timezone.utc)

    return local_now.date() > target_date


def _grade_bet_against_venue(
    bet: dict,
    status_data: dict | None,
    venue: str,
) -> dict | None:
    """Grade a binary weather contract from its exact venue's final result."""
    if not status_data or status_data.get("resolved") is not True:
        return None
    winner = str(status_data.get("winner") or "").lower()
    backed = str(bet.get("outcome_name") or "yes").lower()
    if winner not in {"yes", "no"} or backed not in {"yes", "no"}:
        return None
    market_id = str(bet.get("market_id") or "")
    reported_market_id = str(status_data.get("market_id") or market_id)
    if not market_id or reported_market_id != market_id:
        return None
    try:
        stake = float(bet.get("stake") or 0.0)
        shares = float(bet.get("shares") or 0.0)
    except (TypeError, ValueError):
        return None
    won = winner == backed
    return {
        "status": "won" if won else "lost",
        "pnl": round(shares - stake, 2) if won else round(-stake, 2),
        "version": WEATHER_SETTLEMENT_VERSION,
        "source": f"{venue}_official",
        "venue": venue,
        "market_id": market_id,
        "official_result": winner,
        "settled_at": status_data.get("settled_at"),
        "settlement_value": status_data.get("settlement_value"),
        "rules_primary": status_data.get("rules_primary"),
        "title": status_data.get("title"),
    }


def _apply_resolution(
    db,
    bet: dict,
    new_status: str,
    new_pnl: float,
    now: datetime,
    note: str,
    resolution_source: str | None = None,
    settlement_evidence: dict | None = None,
    correction: bool = False,
) -> bool:
    meta = bet.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta = dict(meta)
    src = resolution_source
    if not src:
        if "observed temp" in (note or ""):
            src = "station_actual"
        elif (note or "").startswith("exchange:"):
            src = note
        else:
            src = note or "unknown"
    meta["resolution_source"] = src
    settlement_version = None
    if settlement_evidence:
        evidence = dict(settlement_evidence)
        evidence.pop("status", None)
        evidence.pop("pnl", None)
        evidence.setdefault("source", src)
        evidence["graded_at"] = now.isoformat()
        meta["settlement"] = evidence
        settlement_version = evidence.get("version")
    update = {
        "status": new_status,
        "pnl": new_pnl,
        "metadata": meta,
    }
    if correction:
        update["settlement_corrected_at"] = now.isoformat()
    else:
        update["resolved_at"] = now.isoformat()
    if settlement_version:
        update["settlement_version"] = settlement_version
    try:
        db.table("autobets").update(update).eq("id", bet["id"]).execute()
        logger.info(
            f"Resolved weather bet {bet['id'][:8]} [{bet.get('market_id')}] "
            f"-> {new_status} (PnL: {new_pnl}; {note}; source={src})"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to update weather bet {bet['id']}: {e}")
        return False


async def resolve_weather_autobets() -> int:
    logger.info("Starting weather autobet resolution...")
    db = get_db()

    # Fetch all open weather bets
    open_bets = (
        db.table("autobets")
        .select("*")
        .eq("bet_type", "weather")
        .eq("status", "open")
        .execute()
        .data or []
    )

    if not open_bets:
        logger.info("No open weather bets found.")
        return 0

    logger.info(f"Checking resolution for {len(open_bets)} open weather bets.")
    resolved_count = 0
    now = datetime.now(timezone.utc)

    for bet in open_bets:
        market_id = bet.get("market_id") or ""

        # Auto-detect venue from market_id format (no 'venue' column in DB yet)
        stored_venue = bet.get("venue") or ""  # might not exist → empty string
        venue = stored_venue.lower() if stored_venue else _detect_venue(market_id)

        status_data = None
        if venue == "kalshi":
            status_data = await check_kalshi_resolution(market_id)
        else:
            # Polymarket (slug-based)
            status_data = await check_polymarket_resolution(market_id)

        # A traded weather contract is graded only from its exact venue result.
        graded = _grade_bet_against_venue(bet, status_data, venue)
        if graded is not None:
            if _apply_resolution(
                db,
                bet,
                graded["status"],
                graded["pnl"],
                now,
                f"exchange:{venue}",
                resolution_source=graded["source"],
                settlement_evidence=graded,
            ):
                resolved_count += 1
            continue
        # Fail open in the ledger (and closed for duplicate execution) until the
        # exact venue publishes a final result. METAR and age are not substitutes
        # for the contract's published resolution source.
        target = _target_date_for_bet(bet)
        if target is not None and (now - target).total_seconds() >= 7 * 86400:
            logger.warning(
                f"Weather bet {bet.get('id')} remains unresolved 7+ days after "
                f"{target.date()}; awaiting {venue} official result"
            )
        continue
    logger.info(f"Resolved {resolved_count} weather bets.")
    return resolved_count


if __name__ == "__main__":
    asyncio.run(resolve_weather_autobets())
