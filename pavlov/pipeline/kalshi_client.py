"""
pipeline/kalshi_client.py – Kalshi REST API client.

BASE_URL: https://external-api.kalshi.com/trade-api/v2

Authentication: RSA request signing
  Every request is signed with the RSA private key loaded from
  KALSHI_PRIVATE_KEY_PATH.  Three headers are added per request:
    KALSHI-Access-Key          – the API key UUID (KALSHI_API_KEY)
    KALSHI-Access-Timestamp    – current time in milliseconds (string)
    KALSHI-Access-Signature    – base64( RSA-PKCS1v15-SHA256(
                                    timestamp_ms + METHOD + /path ) )

Public API
----------
get_weather_markets()  -> list[dict]
get_account_balance()  -> float
market_position_net_contracts(row) -> float  # signed YES/NO from API row
get_open_positions()   -> list[dict]
get_market_as_parsed(ticker) -> dict | None
place_order(ticker, side, contracts, price_cents) -> dict
get_market_result(ticker) -> str | None
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from config import CONFIG
import data_paths as dp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

_CACHE_FILE = os.path.join(dp.data_dir(), "market_cache.json")

_MARKET_CACHE_TTL_SECONDS = 20 * 60           # 20 minutes
_CLOSE_WINDOW_HOURS       = 60                # 2.5 days — catches Day 1, 2, 3 markets
_ORDER_POLL_TIMEOUT       = 30                # seconds
_ORDER_POLL_INTERVAL      = 2                 # seconds

# All confirmed active Kalshi weather series tickers.
# Two naming schemes exist:
#   KXHIGH{CITY} / KXLOW{CITY}  — title contains the city name explicitly
#   KXHIGHT{CITY} / KXLOWT{CITY} — title says only "maximum/minimum temperature";
#                                   city must be inferred from the series ticker.
_WEATHER_SERIES_TICKERS: list[str] = [
    # KXHIGH* — city named in title
    "KXHIGHNY",   "KXHIGHCHI",  "KXHIGHMIA",  "KXHIGHDEN",
    "KXHIGHLAX",  "KXHIGHPHIL", "KXHIGHAUS",
    # KXHIGHT* — "maximum temperature" titles (city from series ticker only)
    "KXHIGHTDC",  "KXHIGHTPHX", "KXHIGHTDAL", "KXHIGHTHOU",
    "KXHIGHTMIN", "KXHIGHTSEA", "KXHIGHTLV",  "KXHIGHTBOS",
    "KXHIGHTOKC", "KXHIGHTATL", "KXHIGHTSFO", "KXHIGHTSATX",
    # KXLOWT* — "minimum temperature" titles (city from series ticker only)
    "KXLOWTNYC",  "KXLOWTCHI",  "KXLOWTMIA",  "KXLOWTDEN",
    "KXLOWTDC",   "KXLOWTLAX",  "KXLOWTDAL",  "KXLOWTLV",
    "KXLOWTMIN",  "KXLOWTSFO",  "KXLOWTATL",  "KXLOWTBOS",
    "KXLOWTSEA",  "KXLOWTPHX",  "KXLOWTHOU",  "KXLOWTOKC",
    "KXLOWTSATX", "KXLOWTPHIL", "KXLOWTAUS",
]

# Maps series ticker → canonical city name (used when the title has no city).
_SERIES_CITY_MAP: dict[str, str] = {
    # KXHIGH* (city in title, but map anyway for safety)
    "KXHIGHNY":    "New York",
    "KXHIGHCHI":   "Chicago",
    "KXHIGHMIA":   "Miami",
    "KXHIGHDEN":   "Denver",
    "KXHIGHLAX":   "Los Angeles",
    "KXHIGHPHIL":  "Philadelphia",
    "KXHIGHAUS":   "Austin",
    # KXHIGHT* (title has no city name)
    "KXHIGHTDC":   "Washington DC",
    "KXHIGHTPHX":  "Phoenix",
    "KXHIGHTDAL":  "Dallas",
    "KXHIGHTHOU":  "Houston",
    "KXHIGHTMIN":  "Minneapolis",
    "KXHIGHTSEA":  "Seattle",
    "KXHIGHTLV":   "Las Vegas",
    "KXHIGHTBOS":  "Boston",
    "KXHIGHTOKC":  "Oklahoma City",
    "KXHIGHTATL":  "Atlanta",
    "KXHIGHTSFO":  "San Francisco",
    "KXHIGHTSATX": "San Antonio",
    # KXLOWT*
    "KXLOWTNYC":   "New York",
    "KXLOWTCHI":   "Chicago",
    "KXLOWTMIA":   "Miami",
    "KXLOWTDEN":   "Denver",
    "KXLOWTDC":    "Washington DC",
    "KXLOWTLAX":   "Los Angeles",
    "KXLOWTDAL":   "Dallas",
    "KXLOWTLV":    "Las Vegas",
    "KXLOWTMIN":   "Minneapolis",
    "KXLOWTSFO":   "San Francisco",
    "KXLOWTATL":   "Atlanta",
    "KXLOWTBOS":   "Boston",
    "KXLOWTSEA":   "Seattle",
    "KXLOWTPHX":   "Phoenix",
    "KXLOWTHOU":   "Houston",
    "KXLOWTOKC":   "Oklahoma City",
    "KXLOWTSATX":  "San Antonio",
    "KXLOWTPHIL":  "Philadelphia",
    "KXLOWTAUS":   "Austin",
}

# Maps series ticker → metric ('high' or 'low').
_SERIES_METRIC_MAP: dict[str, str] = {
    **{t: "high" for t in _WEATHER_SERIES_TICKERS if "HIGH" in t},
    **{t: "low"  for t in _WEATHER_SERIES_TICKERS if "LOW"  in t},
}


def format_open_position_embed_field(
    ticker: str,
    side: str,
    n_contracts: float,
    exposure_dollars,
) -> tuple[str, str]:
    """Return (embed field name, embed field value) for a Kalshi position row."""
    parts = (ticker or "").split("-")
    series = parts[0] if parts else ""
    city = _SERIES_CITY_MAP.get(series, "")
    metric = _SERIES_METRIC_MAP.get(series, "")
    date_code = parts[1] if len(parts) > 1 else ""
    strike = "-".join(parts[2:]) if len(parts) > 2 else ""
    mlab = (metric[:1].upper() + metric[1:]) if metric else ""
    title_bits = [x for x in (city, mlab, date_code) if x]
    title = (
        " · ".join(title_bits)
        if title_bits
        else ((ticker or "?")[:60] + ("…" if len(ticker or "") > 60 else ""))
    )
    try:
        exp_f = float(exposure_dollars)
    except (TypeError, ValueError):
        exp_f = 0.0
    strike_line = f" · strike `{strike}`" if strike else ""
    value = (
        f"**{side}** {n_contracts:g}× · ${exp_f:,.2f}{strike_line}\n"
        f"`{ticker}`"
    )
    return title, value


# ---------------------------------------------------------------------------
# Session + RSA private key – initialised once at module load
# ---------------------------------------------------------------------------

_session: requests.Session = requests.Session()


def _load_private_key() -> RSAPrivateKey:
    """Load the RSA private key from env var or file path.

    Priority:
      1. KALSHI_PRIVATE_KEY_B64  – base64-encoded PEM (preferred for cloud).
         Set this on Railway/Render/etc. instead of mounting a file.
      2. KALSHI_PRIVATE_KEY_PATH – path to a .pem file (local development).

    Handles bare-base64 files, CRLF line endings, and UTF-8 BOM.
    """
    # 1. Try the env-var route first (cloud-friendly).
    b64_env = os.environ.get("KALSHI_PRIVATE_KEY_B64", "").strip()
    if b64_env:
        try:
            pem_bytes = base64.b64decode(b64_env)
            key = serialization.load_pem_private_key(pem_bytes, password=None)
            logger.info("KalshiClient: RSA private key loaded from KALSHI_PRIVATE_KEY_B64 env var.")
            return key  # type: ignore[return-value]
        except Exception as exc:
            raise ValueError(
                f"KalshiClient: KALSHI_PRIVATE_KEY_B64 is set but could not be decoded.\n"
                f"  Error: {exc}\n"
                "  Ensure it is the full PEM content base64-encoded (no line breaks)."
            ) from exc

    # 2. Fall back to file path.
    raw_path: str = CONFIG["KALSHI_PRIVATE_KEY_PATH"]
    if not os.path.isabs(raw_path):
        raw_path = os.path.join(dp.app_root(), raw_path.lstrip("./\\"))
    if not os.path.exists(raw_path):
        raise FileNotFoundError(
            f"KalshiClient: private key not found at {raw_path!r}. "
            "Set KALSHI_PRIVATE_KEY_PATH in your .env file, or set "
            "KALSHI_PRIVATE_KEY_B64 for cloud deployments."
        )

    with open(raw_path, "rb") as fh:
        raw_bytes = fh.read()

    # Strip UTF-8 BOM that Windows text editors sometimes prepend.
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]

    # Normalise CRLF → LF.
    raw_bytes = raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    text = raw_bytes.decode("ascii", errors="ignore").strip()

    # If the file has no PEM header, treat content as raw base64 and wrap it.
    if not text.startswith("-----"):
        logger.warning(
            "KalshiClient: PEM file has no header — wrapping bare base64 content."
        )
        b64_clean = "".join(text.split())
        lines = [b64_clean[i:i+64] for i in range(0, len(b64_clean), 64)]
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + "\n".join(lines)
            + "\n-----END RSA PRIVATE KEY-----\n"
        )

    pem_bytes = text.encode("utf-8")

    try:
        key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as exc:
        raise ValueError(
            f"KalshiClient: failed to load RSA private key from {raw_path!r}.\n"
            f"  Error     : {exc}\n"
            f"  PEM start : {text[:120]!r}"
        ) from exc

    if not isinstance(key, RSAPrivateKey):
        raise TypeError(
            f"KalshiClient: expected RSAPrivateKey, got {type(key).__name__}."
        )
    logger.info("KalshiClient: RSA private key loaded (%d-bit) from %s.",
                key.key_size, raw_path)
    return key





_private_key: RSAPrivateKey = _load_private_key()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(dt_str: str) -> datetime:
    """Parse an ISO-8601 string that may end with 'Z' or an offset."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def _sign_request(method: str, path: str) -> dict[str, str]:
    """Return the three Kalshi RSA-signing headers for one request.

    Message format (no separator):
        str(timestamp_milliseconds) + METHOD_UPPER + path

    Example: "1747441200000GET/trade-api/v2/markets"

    Signing: RSA-PSS with MGF1(SHA-256) and salt_length=DIGEST_LENGTH,
    then base64-encoded — exactly as specified in the Kalshi API docs.

    Args:
        method: HTTP verb, e.g. 'GET' or 'POST'.
        path:   Full URL path including /trade-api/v2 prefix.
                Query strings must be stripped before calling.

    Returns:
        Dict with uppercase Kalshi auth header names.
    """
    timestamp_ms = str(int(time.time() * 1000))
    message = (timestamp_ms + method.upper() + path).encode("utf-8")

    signature_bytes = _private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    signature_b64 = base64.b64encode(signature_bytes).decode("utf-8")

    return {
        "KALSHI-ACCESS-KEY":       CONFIG["KALSHI_API_KEY"],
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature_b64,
    }


# ---------------------------------------------------------------------------
# Low-level request wrappers
# ---------------------------------------------------------------------------

# Signing uses the FULL path including /trade-api/v2 prefix (per Kalshi docs).
# Query strings must be stripped before signing.
_API_PATH_PREFIX = "/trade-api/v2"

# Retry / rate-limit settings.
_MAX_RETRIES       = 4
_RETRY_CODES       = {429, 500, 502, 503, 504}
# Backoff schedule in seconds: attempt 0 → 10 s, 1 → 30 s, 2 → 60 s
_BACKOFF_SCHEDULE  = [10, 30, 60, 60]
# Minimum gap between consecutive outbound requests (seconds).
_MIN_REQUEST_GAP   = 1.0

# Tracks the monotonic time of the last request so we never fire faster
# than _MIN_REQUEST_GAP regardless of the code path taken.
_last_request_time: float = 0.0


def _rate_limit() -> None:
    """Block until at least _MIN_REQUEST_GAP seconds have passed since the
    last API call.  Keeps us comfortably inside Kalshi's rate limit."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_REQUEST_GAP:
        time.sleep(_MIN_REQUEST_GAP - elapsed)
    _last_request_time = time.monotonic()


def _backoff_wait(attempt: int, resp) -> None:
    """Sleep before the next retry attempt.

    Reads ``Retry-After`` from the response headers first.  If absent, uses
    the fixed schedule: 10 s → 30 s → 60 s → 60 s.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            wait = max(float(retry_after), _BACKOFF_SCHEDULE[attempt])
        except ValueError:
            wait = _BACKOFF_SCHEDULE[attempt]
    else:
        wait = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]

    logger.warning(
        "KalshiClient: HTTP %d – retrying in %.0f s (attempt %d/%d) …",
        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
    )
    time.sleep(wait)


def _get(path: str, params: dict | None = None) -> Any:
    """Signed GET request with rate-limiting and automatic retry on 429/5xx.

    *path* is the suffix after BASE_URL, e.g. '/markets'.
    """
    url = f"{BASE_URL}{path}"
    for attempt in range(_MAX_RETRIES):
        _rate_limit()
        # Re-sign on every attempt so the timestamp is always fresh.
        signed_path = _API_PATH_PREFIX + path
        headers = _sign_request("GET", signed_path)
        resp = _session.get(url, params=params, headers=headers, timeout=20)

        if resp.status_code == 401:
            logger.error(
                "KalshiClient: 401 on GET %s\n"
                "  Signed path : %s\n"
                "  Timestamp   : %s\n"
                "  Response    : %s",
                path,
                signed_path,
                headers.get("KALSHI-Access-Timestamp"),
                resp.text[:500],
            )
            raise PermissionError(
                f"KalshiClient: 401 Unauthorized on GET {path}.\n"
                f"  Kalshi says: {resp.text[:300]}"
            )
        if resp.status_code in _RETRY_CODES and attempt < _MAX_RETRIES - 1:
            _backoff_wait(attempt, resp)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"KalshiClient: _get({path!r}) exhausted all retries.")


def _post(path: str, body: dict) -> Any:
    """Signed POST request with rate-limiting and automatic retry on 429/5xx.

    *path* is the suffix after BASE_URL.
    """
    url = f"{BASE_URL}{path}"
    for attempt in range(_MAX_RETRIES):
        _rate_limit()
        signed_path = _API_PATH_PREFIX + path
        headers = _sign_request("POST", signed_path)
        resp = _session.post(url, json=body, headers=headers, timeout=20)

        if resp.status_code == 401:
            logger.error(
                "KalshiClient: 401 on POST %s\n"
                "  Signed path : %s\n"
                "  Timestamp   : %s\n"
                "  Response    : %s",
                path,
                signed_path,
                headers.get("KALSHI-Access-Timestamp"),
                resp.text[:500],
            )
            raise PermissionError(
                f"KalshiClient: 401 Unauthorized on POST {path}.\n"
                f"  Kalshi says: {resp.text[:300]}"
            )
        if resp.status_code in _RETRY_CODES and attempt < _MAX_RETRIES - 1:
            _backoff_wait(attempt, resp)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"KalshiClient: _post({path!r}) exhausted all retries.")


# ---------------------------------------------------------------------------
# Market cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data: list[dict]) -> None:
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    payload = {
        "fetched_at": _utcnow().isoformat(),
        "markets": data,
    }
    with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)


def _cache_is_fresh(cache: dict) -> bool:
    fetched_str = cache.get("fetched_at")
    if not fetched_str:
        return False
    try:
        fetched_at = _parse_iso(fetched_str)
        age = (_utcnow() - fetched_at).total_seconds()
        return age < _MARKET_CACHE_TTL_SECONDS
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------------

def _parse_market(raw: dict, series_ticker: str = "") -> dict:
    """Flatten a raw Kalshi market object into the canonical schema.

    Kalshi v2 returns prices as dollar strings (e.g. ``yes_ask_dollars``).
    We convert them to integer cents so the rest of the pipeline can use
    plain integer arithmetic (50 = 50¢ = $0.50).

    ``strike_type`` is stored verbatim ('greater', 'less', 'between') and is
    the authoritative direction — more reliable than parsing title symbols.

    ``city_hint`` and ``metric_hint`` are derived from the series ticker so
    that markets whose titles omit the city name (KXHIGHT*/KXLOWT* series)
    can still be correctly attributed.
    """
    def _dollars_to_cents(val) -> int | None:
        try:
            return round(float(val) * 100)
        except (TypeError, ValueError):
            return None

    return {
        "ticker":        raw.get("ticker", ""),
        "title":         raw.get("title", ""),
        "strike_type":   raw.get("strike_type", ""),   # 'greater'|'less'|'between'
        "floor_strike":  raw.get("floor_strike"),       # lower bound int
        "yes_ask":       _dollars_to_cents(raw.get("yes_ask_dollars")),
        "yes_bid":       _dollars_to_cents(raw.get("yes_bid_dollars")),
        "no_ask":        _dollars_to_cents(raw.get("no_ask_dollars")),
        "no_bid":        _dollars_to_cents(raw.get("no_bid_dollars")),
        "close_time":    raw.get("close_time", ""),
        "volume":        raw.get("volume_fp", raw.get("volume", 0)),
        "open_interest": raw.get("open_interest_fp", raw.get("open_interest", 0)),
        # Series-derived hints for KXHIGHT*/KXLOWT* markets (title has no city).
        "series_ticker": series_ticker,
        "city_hint":     _SERIES_CITY_MAP.get(series_ticker, ""),
        "metric_hint":   _SERIES_METRIC_MAP.get(series_ticker, ""),
    }


def _closes_within_window(market: dict) -> bool:
    """Return True if close_time is between now and now+36 hours."""
    close_str = market.get("close_time", "")
    if not close_str:
        return False
    try:
        close_dt = _parse_iso(close_str)
        now = _utcnow()
        cutoff = now + timedelta(hours=_CLOSE_WINDOW_HOURS)
        return now <= close_dt <= cutoff
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_weather_markets() -> list[dict]:
    """Return open weather markets closing within the next 36 hours.

    Kalshi weather markets are NOT surfaced by the generic /markets endpoint.
    This function iterates ``_WEATHER_SERIES_TICKERS`` and queries each series
    individually, then deduplicates and applies the 36-hour close window filter.

    Results are cached in ``data/market_cache.json`` for 20 minutes.
    """
    cache = _load_cache()
    if _cache_is_fresh(cache) and "markets" in cache:
        logger.info(
            "KalshiClient: returning %d markets from cache.",
            len(cache["markets"]),
        )
        return cache["markets"]

    logger.info(
        "KalshiClient: fetching weather markets from %d series tickers ...",
        len(_WEATHER_SERIES_TICKERS),
    )

    seen_tickers: set[str] = set()
    all_markets: list[dict] = []

    for series_ticker in _WEATHER_SERIES_TICKERS:
        try:
            data = _get("/markets", params={
                "status":        "open",
                "series_ticker": series_ticker,
                "limit":         100,
            })
        except Exception as exc:
            logger.debug(
                "KalshiClient: skipping series %s - %s", series_ticker, exc
            )
            continue

        for raw in data.get("markets", []):
            m = _parse_market(raw, series_ticker=series_ticker)
            if m["ticker"] not in seen_tickers:
                seen_tickers.add(m["ticker"])
                all_markets.append(m)

    filtered = [m for m in all_markets if _closes_within_window(m)]

    logger.info(
        "KalshiClient: %d weather markets found -> %d closing within %dh.",
        len(all_markets),
        len(filtered),
        _CLOSE_WINDOW_HOURS,
    )
    _save_cache(filtered)
    return filtered



def get_account_balance() -> float:
    """Return the available balance in dollars (float)."""
    data = _get("/portfolio/balance")
    # Kalshi returns balance in cents as an integer field.
    balance_field = (
        data.get("available_balance")
        or data.get("balance")
        or 0
    )
    dollars = float(balance_field) / 100.0
    logger.info("KalshiClient: account balance = $%.2f", dollars)
    return dollars


def market_position_net_contracts(p: dict) -> float:
    """Signed net contracts from a *market_positions* row.

    Kalshi v2 uses ``position_fp`` (string, e.g. ``\"5.00\"``): positive = YES,
    negative = NO. Falls back to legacy integer ``position`` when present.
    """
    fp = p.get("position_fp")
    if fp is not None and str(fp).strip() != "":
        try:
            return float(fp)
        except (TypeError, ValueError):
            pass
    leg = p.get("position")
    if leg is None:
        return 0.0
    try:
        return float(leg)
    except (TypeError, ValueError):
        return 0.0


def get_open_positions() -> list[dict]:
    """Return a list of open market positions.

    Each row is a Kalshi ``MarketPosition`` dict (``ticker``, ``position_fp``,
    ``market_exposure_dollars``, etc.).
    """
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            data = _get("/portfolio/positions")
            positions: list[dict] = data.get("market_positions", [])
            open_pos = [p for p in positions if market_position_net_contracts(p) != 0]
            logger.info("KalshiClient: %d open positions.", len(open_pos))
            return open_pos
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "KalshiClient: get_open_positions attempt %d failed — %s",
                attempt + 1,
                exc,
            )
            if attempt == 0:
                time.sleep(1.0)
    assert last_exc is not None
    raise last_exc


def get_market_as_parsed(ticker: str) -> dict | None:
    """Fetch one market and return the same dict shape as ``get_weather_markets`` rows.

    Used when rebuilding city/date dedup from exchange positions that are not
    in the current weather scan list.
    """
    try:
        data = _get(f"/markets/{ticker}")
    except Exception as exc:
        logger.debug("KalshiClient: get_market_as_parsed(%s) — %s", ticker, exc)
        return None
    raw = data.get("market", data)
    if not isinstance(raw, dict) or not raw.get("ticker"):
        return None
    series = str(raw.get("series_ticker", "") or "")
    return _parse_market(raw, series_ticker=series)


def place_order(
    ticker: str,
    side: str,             # "yes" or "no"
    contracts: int,
    price_cents: int,      # limit price in cents (1–99)
) -> dict:
    """Place a limit buy order and poll for fill for up to 30 seconds.

    Args:
        ticker:       Market ticker, e.g. 'KXHIGHNY-26MAY17-T84'.
        side:         'yes' or 'no'.
        contracts:    Number of contracts to purchase.
        price_cents:  Limit price in cents (integer 1–99).

    Returns:
        {
            "order_id":         str,
            "status":           str,   # e.g. "filled", "resting", "canceled"
            "filled_contracts": int,
            "price":            int,   # cents
        }

    Raises:
        ValueError:  if side is not 'yes' or 'no'.
        requests.HTTPError: on API errors.
    """
    import uuid
    if side not in ("yes", "no"):
        raise ValueError(f"place_order: side must be 'yes' or 'no', got {side!r}.")

    if not (1 <= price_cents <= 99):
        raise ValueError(
            f"place_order: price_cents must be 1–99, got {price_cents}. "
            "Did you pass a dollar amount instead of cents?"
        )

    body: dict[str, Any] = {
        "ticker":           ticker,
        "client_order_id":  str(uuid.uuid4()),   # idempotency key
        "action":           "buy",
        "side":             side,
        "type":             "limit",
        "count":            contracts,
    }

    # yes_price is always in cents (integer) per the Kalshi v2 API.
    # For NO orders the effective cost is (100 - yes_price) cents.
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents

    logger.info(
        "KalshiClient: placing %s %s × %d @ %d¢ on %s",
        side.upper(), ticker, contracts, price_cents, ticker,
    )

    resp_data = _post("/portfolio/orders", body)
    order_raw: dict = resp_data.get("order", resp_data)
    order_id: str = order_raw.get("order_id", "")

    if not order_id:
        logger.error("KalshiClient: no order_id in response: %s", resp_data)
        return {
            "order_id":         "",
            "status":           "error",
            "filled_contracts": 0,
            "price":            price_cents,
        }

    # ------------------------------------------------------------------
    # Poll for fill (up to _ORDER_POLL_TIMEOUT seconds)
    # ------------------------------------------------------------------
    deadline = time.monotonic() + _ORDER_POLL_TIMEOUT
    status   = order_raw.get("status", "resting")
    filled   = order_raw.get("filled_count", 0)

    while status not in ("filled", "canceled", "expired") and time.monotonic() < deadline:
        time.sleep(_ORDER_POLL_INTERVAL)
        try:
            poll = _get(f"/portfolio/orders/{order_id}")
            order_raw = poll.get("order", poll)
            status    = order_raw.get("status", status)
            filled    = order_raw.get("filled_count", filled)
            logger.debug(
                "KalshiClient: order %s status=%s filled=%d",
                order_id, status, filled,
            )
        except Exception as exc:
            logger.warning("KalshiClient: poll error for %s – %s", order_id, exc)
            break

    result = {
        "order_id":         order_id,
        "status":           status,
        "filled_contracts": filled,
        "price":            price_cents,
    }
    logger.info(
        "KalshiClient: order %s final status=%s filled=%d/%d",
        order_id, status, filled, contracts,
    )
    return result


def get_market_result(ticker: str) -> str | None:
    """Return 'yes' or 'no' if the market has settled, else None.

    Args:
        ticker: Market ticker string.

    Returns:
        'yes', 'no', or None (market not yet settled).
    """
    data = _get(f"/markets/{ticker}")
    market: dict = data.get("market", data)
    status: str = market.get("status", "").lower()

    if status == "settled":
        result: str = market.get("result", "").lower()
        if result in ("yes", "no"):
            logger.info("KalshiClient: %s settled → %s", ticker, result)
            return result
        logger.warning(
            "KalshiClient: %s settled but unrecognised result %r", ticker, result
        )
        return None

    logger.debug("KalshiClient: %s status=%s (not settled)", ticker, status)
    return None
