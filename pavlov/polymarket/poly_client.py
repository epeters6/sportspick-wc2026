"""
polymarket/poly_client.py – Polymarket US API via official polymarket-us SDK.

Normalizes markets into Kalshi-shaped dicts so pipeline/signal_engine.calculate_edge
can run unchanged.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any
from datetime import datetime, timezone
from types import SimpleNamespace

try:
    from pavlov.config import CONFIG
except ModuleNotFoundError as exc:
    if exc.name != "pavlov":
        raise
    from config import CONFIG  # Standalone Pavlov deployment.

logger = logging.getLogger(__name__)

_EVENT_END_CACHE: dict[str, str] = {}

# Lazy client singleton
_client: Any = None
_public_client: Any = None


def get_public_client():
    """Credential-free public market resources, separate from the trading client."""
    global _public_client
    if _public_client is None:
        from polymarket_us import PolymarketUS

        public = PolymarketUS()
        _public_client = SimpleNamespace(markets=public.markets, events=public.events)
    return _public_client


def poly_configured() -> bool:
    kid = str(CONFIG.get("POLY_KEY_ID") or CONFIG.get("POLYMARKET_KEY_ID") or "").strip()
    sec = str(CONFIG.get("POLY_SECRET_KEY") or CONFIG.get("POLYMARKET_SECRET_KEY") or "").strip()
    return bool(kid and sec)


def get_client():
    """Return a synchronous PolymarketUS client (or raise if not configured)."""
    global _client
    if _client is not None:
        return _client
    if not poly_configured():
        raise RuntimeError("Polymarket US credentials missing — set POLY_KEY_ID and POLY_SECRET_KEY")
    from polymarket_us import PolymarketUS

    key_id = str(CONFIG.get("POLY_KEY_ID") or CONFIG.get("POLYMARKET_KEY_ID"))
    secret = str(CONFIG.get("POLY_SECRET_KEY") or CONFIG.get("POLYMARKET_SECRET_KEY"))
    _client = PolymarketUS(key_id=key_id, secret_key=secret)
    return _client


def _amount_to_prob(amt) -> float | None:
    if amt is None:
        return None
    if isinstance(amt, (int, float)):
        try:
            return float(amt)
        except (TypeError, ValueError):
            return None
    if not isinstance(amt, dict):
        return None
    raw = amt.get("value")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _qty_to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value") or value.get("qty") or value.get("size")
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    return qty if qty >= 0 else None


def _exchange_timestamp(raw: dict) -> Any:
    """Return only a timestamp supplied by the venue, never local receipt time."""
    for key in ("orderbook_timestamp", "timestamp", "ts", "updatedAt", "updated_at"):
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _unwrap_gateway_market_blob(raw: Any) -> dict[str, Any]:
    """Polymarket US gateway wraps payload in ``marketData`` (BBO, book, etc.)."""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("marketData")
    if isinstance(inner, dict):
        return dict(inner)
    return dict(raw)


def _yes_probs_from_bbo_dict(bbo: dict) -> tuple[float | None, float | None]:
    """Derive YES bid/ask probabilities (0–1) from a BBO-ish dict.

    Polymarket US often returns null ``bestBid``/``bestAsk`` for thin event lines
    while ``lastTradePx`` is still populated — use that as a mid with a small
    synthetic spread so ``signal_engine`` can run.
    """
    bid = _amount_to_prob(bbo.get("bestBid"))
    ask = _amount_to_prob(bbo.get("bestAsk"))
    if bid is not None or ask is not None:
        return bid, ask
    ltp = _amount_to_prob(
        bbo.get("lastTradePx")
        or bbo.get("lastTradePrice")
        or bbo.get("currentPx")
    )
    if ltp is not None:
        half = max(0.005, min(ltp * 0.02, 0.04))
        b = max(0.01, ltp - half)
        a = min(0.99, ltp + half)
        if b >= a:
            a = min(0.99, b + 0.01)
        return b, a
    return None, None


def _bbo_from_book(book: Any) -> dict[str, Any] | None:
    """Top-of-book bid/ask from ``/v1/markets/{slug}/book``."""
    if not book or not isinstance(book, dict):
        return None
    raw_book = dict(book)
    book = _unwrap_gateway_market_blob(book)
    bids = book.get("bids") or []
    offers = book.get("offers") or []
    out: dict[str, Any] = {}
    parsed_bids = [
        (_amount_to_prob(level.get("px")), _qty_to_float(level.get("qty")))
        for level in bids
        if isinstance(level, dict)
    ]
    parsed_bids = [(p, q) for p, q in parsed_bids if p is not None]
    if parsed_bids:
        best_bid = max(p for p, _ in parsed_bids)
        bid_size = sum((q or 0.0) for p, q in parsed_bids if abs(p - best_bid) < 1e-9)
        out["bestBid"] = {"value": str(best_bid)}
        out["yes_bid_size"] = bid_size

    parsed_offers = [
        (_amount_to_prob(level.get("px")), _qty_to_float(level.get("qty")))
        for level in offers
        if isinstance(level, dict)
    ]
    parsed_offers = [(p, q) for p, q in parsed_offers if p is not None]
    if parsed_offers:
        best_ask = min(p for p, _ in parsed_offers)
        ask_size = sum((q or 0.0) for p, q in parsed_offers if abs(p - best_ask) < 1e-9)
        out["bestAsk"] = {"value": str(best_ask)}
        out["yes_ask_size"] = ask_size
        out["yes_ask_qty"] = ask_size
        out["ask_size"] = ask_size
    venue_ts = _exchange_timestamp(book) or _exchange_timestamp(raw_book)
    if venue_ts is not None:
        out["orderbook_timestamp"] = venue_ts
    out["received_timestamp"] = datetime.now(timezone.utc)
    out["execution_price_source"] = "orderbook"
    return out or None


def _build_price_bbo(client, m: dict, slug: str) -> dict[str, Any]:
    """Collect bestBid/bestAsk/lastTradePx from row, BBO API, then order book."""
    row_bbo = _bbo_dict_from_list_row(m) or {}
    ltp_raw = m.get("lastTradePrice") or m.get("lastTradePx")
    stats = m.get("stats")
    if isinstance(stats, dict):
        ltp_raw = ltp_raw or stats.get("lastTradePx") or stats.get("lastTradePrice")
    if ltp_raw is not None and not row_bbo.get("bestBid") and not row_bbo.get("bestAsk"):
        row_bbo = dict(row_bbo)
        row_bbo["lastTradePx"] = (
            ltp_raw if isinstance(ltp_raw, dict) else {"value": str(ltp_raw)}
        )

    merged: dict[str, Any] = {}
    merged.update(row_bbo)

    try:
        api = client.markets.bbo(slug)
        inner = _unwrap_gateway_market_blob(api)
        if inner:
            for k, v in inner.items():
                if v is not None:
                    merged[k] = v
    except Exception as exc:
        logger.debug("PolyClient: bbo(%s) failed — %s", slug, exc)

    bid, ask = _yes_probs_from_bbo_dict(merged)
    ask_size = _qty_to_float(
        merged.get("yes_ask_size")
        or merged.get("yes_ask_qty")
        or merged.get("ask_size")
        or merged.get("bestAskQty")
        or merged.get("bestOfferQty")
    )
    # BBO may provide price but not executable quantity. Fetch the full book
    # whenever either price or ask depth is absent.
    if (bid is None and ask is None) or not ask_size:
        try:
            book = client.markets.book(slug)
            book_patch = _bbo_from_book(book)
            if book_patch:
                merged.update(book_patch)
        except Exception as exc:
            logger.debug("PolyClient: book(%s) failed — %s", slug, exc)

    merged.setdefault("received_timestamp", datetime.now(timezone.utc))
    return merged


def _poly_outcome_text(m: dict) -> str:
    """Outcome / line label (e.g. ``85 to 86``) from a Polymarket market row."""
    for key in (
        "outcome",
        "outcomeTitle",
        "groupItemTitle",
        "shortOutcome",
        "subtitle",
    ):
        v = (m.get(key) or "").strip()
        if v:
            return v
    tit = (m.get("title") or "").strip()
    q = (m.get("question") or "").strip()
    if tit and tit.lower() != q.lower() and tit.lower() not in q.lower():
        return tit
    return ""


def _poly_market_display_text(m: dict) -> str:
    """Text used to detect daily-temperature props (Polymarket US uses *question*; *title* is often empty)."""
    chunks: list[str] = []
    for key in ("title", "question", "subtitle"):
        v = (m.get(key) or "").strip()
        if v:
            chunks.append(v)
    desc = (m.get("description") or "").strip()
    if desc:
        chunks.append(desc[:280])
    return " ".join(chunks) if chunks else ""


def _looks_like_daily_temp(text: str) -> bool:
    t = text.lower()
    if "temperature" not in t and "temp" not in t:
        return False
    if not any(
        w in t
        for w in (
            "high",
            "low",
            "maximum",
            "minimum",
            "max temp",
            "min temp",
        )
    ):
        return False
    # Require a Fahrenheit-ish magnitude or explicit degree wording (API copy varies).
    if re.search(r"\d{2,3}\s*[°˚]?\s*f\b", t, re.I):
        return True
    if "°" in t or "degree" in t or "fahrenheit" in t:
        return True
    if re.search(r"\b\d{2,3}\b", t):
        return True
    return False


def _poly_strike_fields(full_title: str) -> dict[str, Any] | None:
    """Map Polymarket outcome text to Kalshi-style strike fields.

    Handles bracket wordings from the mobile app / API, e.g.
    ``85 to 86``, ``80 or below``, ``87 or above``, plus classic ``>82°F``.
    """
    t = full_title
    tl = full_title.lower()
    
    # Strip out ISO dates (YYYY-MM-DD) so they aren't parsed as temperature ranges
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", t)
    tl = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", tl)

    m = re.search(r"\b(\d{2,3})\s*(?:-|–|—|to)\s*(\d{2,3})\b", tl)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return {
            "strike_type": "between",
            "threshold_lo": lo,
            "threshold_hi": hi,
        }

    m = re.search(r"\b(\d{2,3})\s+or\s+below\b", tl)
    if m:
        return {"strike_type": "less", "ceiling_strike": float(m.group(1))}

    m = re.search(r"\b(\d{2,3})\s+or\s+above\b", tl)
    if m:
        return {"strike_type": "greater", "floor_strike": float(m.group(1))}

    m = re.search(r"[>≥]\s*(\d{2,3})(?:\s*[°˚]?\s*f\b|\b)", t, re.I)
    if m:
        return {"strike_type": "greater", "floor_strike": float(m.group(1))}
    m = re.search(r"[<≤]\s*(\d{2,3})(?:\s*[°˚]?\s*f\b|\b)", t, re.I)
    if m:
        return {"strike_type": "less", "ceiling_strike": float(m.group(1))}

    m = re.search(r">\s*(\d{2,3})", t)
    if m:
        return {"strike_type": "greater", "floor_strike": float(m.group(1))}
    m = re.search(r"<\s*(\d{2,3})", t)
    if m:
        return {"strike_type": "less", "ceiling_strike": float(m.group(1))}

    m = re.search(
        r"\b(?:above|over|exceed|greater\s+than|at\s+least)\s+(\d{2,3})\b",
        tl,
    )
    if m:
        return {"strike_type": "greater", "floor_strike": float(m.group(1))}
    m = re.search(
        r"\b(?:below|under|less\s+than|at\s+most)\s+(\d{2,3})\b",
        tl,
    )
    if m:
        return {"strike_type": "less", "ceiling_strike": float(m.group(1))}

    return None


def get_account_balance() -> float:
    """Return USD buying power (best effort across balance records)."""
    c = get_client()
    resp = c.account.balances()
    balances = resp.get("balances") or []
    for b in balances:
        if str(b.get("currency", "")).upper() == "USD":
            bp = b.get("buyingPower")
            if bp is not None:
                return float(bp)
            cb = b.get("currentBalance")
            if cb is not None:
                return float(cb)
    if balances:
        b0 = balances[0]
        for key in ("buyingPower", "currentBalance", "assetAvailable"):
            v = b0.get(key)
            if v is not None:
                return float(v)
    logger.warning("PolyClient: could not parse balances response — assuming 0")
    return 0.0


def _event_close_time(client, event_slug: str) -> str:
    if not event_slug:
        return ""
    if event_slug in _EVENT_END_CACHE:
        return _EVENT_END_CACHE[event_slug]
    try:
        ev = client.events.retrieve_by_slug(event_slug)
        end = (ev.get("event") or {}).get("endTime") or ""
        if end and not end.endswith("Z") and "+" not in end:
            end = end + "Z"
        _EVENT_END_CACHE[event_slug] = end
        return end
    except Exception as exc:
        logger.debug("PolyClient: event %s endTime fetch failed — %s", event_slug, exc)
        return ""


def _normalize_market_row(
    client,
    m: dict,
    bbo: dict,
    *,
    display_text: str,
) -> dict | None:
    slug = m.get("slug") or ""
    if not slug:
        return None
    outcome = _poly_outcome_text(m)
    head = display_text.strip() or _poly_market_display_text(m)
    full_title = f"{head} ({outcome})" if outcome else head
    if not _looks_like_daily_temp(full_title):
        return None

    strike = _poly_strike_fields(full_title)
    if not strike:
        return None

    bid, ask = _yes_probs_from_bbo_dict(bbo)
    if bid is None and ask is None:
        return None
    # Kalshi-style cents 0–100; signal_engine divides by 100/200
    yes_bid = round(bid * 100, 2) if bid is not None else None
    yes_ask = round(ask * 100, 2) if ask is not None else None
    if yes_ask is None and yes_bid is not None:
        yes_ask = min(99.0, yes_bid + 1.0)
    if yes_bid is None and yes_ask is not None:
        yes_bid = max(0.0, yes_ask - 1.0)
    if yes_ask is None:
        return None

    try:
        oi = int(float(bbo.get("openInterest") or 0))
    except (TypeError, ValueError):
        oi = 0

    event_slug = m.get("eventSlug") or ""
    close_raw = _event_close_time(client, event_slug)
    if not close_raw:
        end = (m.get("endDate") or m.get("end_time") or "").strip()
        if end and not end.endswith("Z") and "+" not in end:
            end = end + "Z"
        close_raw = end

    row: dict[str, Any] = {
        "ticker":           slug,
        "title":            full_title,
        "market_title":     full_title,
        "description":      m.get("description", ""),
        "resolution_source": m.get("resolutionSource") or m.get("resolution_source"),
        "rules_primary":    m.get("rules_primary", ""),
        "settlement_station": m.get("settlement_station") or m.get("station"),
        "yes_bid":          yes_bid,
        "yes_ask":          yes_ask,
        "open_interest":    oi,
        "close_time":       close_raw,
        "strike_type":      strike["strike_type"],
        "floor_strike":     strike.get("floor_strike"),
        "ceiling_strike":   strike.get("ceiling_strike"),
        "threshold_lo":     strike.get("threshold_lo"),
        "threshold_hi":     strike.get("threshold_hi"),
        "volume":           m.get("volume") or 0,
        "liquidity":        m.get("liquidity"),
        "poly_event_slug":  event_slug,
        "poly_market_slug": slug,
        "venue":            "poly_us",
        "yes_ask_size":     _qty_to_float(
            bbo.get("yes_ask_size")
            or bbo.get("yes_ask_qty")
            or bbo.get("ask_size")
            or bbo.get("bestAskQty")
            or bbo.get("bestOfferQty")
        ) or 0.0,
        "yes_ask_qty":      _qty_to_float(
            bbo.get("yes_ask_size")
            or bbo.get("yes_ask_qty")
            or bbo.get("ask_size")
            or bbo.get("bestAskQty")
            or bbo.get("bestOfferQty")
        ) or 0.0,
        "ask_size":         _qty_to_float(
            bbo.get("yes_ask_size")
            or bbo.get("yes_ask_qty")
            or bbo.get("ask_size")
            or bbo.get("bestAskQty")
            or bbo.get("bestOfferQty")
        ) or 0.0,
        "execution_price_source": bbo.get("execution_price_source"),
        "received_timestamp": bbo.get("received_timestamp")
        or m.get("received_timestamp")
        or datetime.now(timezone.utc),
        "orderbook_timestamp": bbo.get("orderbook_timestamp")
        or m.get("orderbook_timestamp"),
    }
    return row


def _markets_list_response_rows(resp: Any) -> list[dict]:
    if resp is None:
        return []
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if not isinstance(resp, dict):
        return []
    for key in ("markets", "data", "results", "items"):
        v = resp.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
    return []


def _events_list_response_rows(resp: Any) -> list[dict]:
    if resp is None or not isinstance(resp, dict):
        return []
    v = resp.get("events")
    if isinstance(v, list):
        return [e for e in v if isinstance(e, dict)]
    return []


def _flatten_event_markets(events: list[dict]) -> list[dict]:
    """Expand /v1/events nested ``markets[]`` into flat rows (weather props live here)."""
    rows: list[dict] = []
    for ev in events:
        markets = ev.get("markets")
        if not isinstance(markets, list):
            continue
        e_slug = ev.get("slug") or ""
        e_title = (ev.get("title") or "").strip()
        e_desc = (ev.get("description") or "").strip()
        e_end = (ev.get("endTime") or ev.get("endDate") or "").strip()
        for m in markets:
            if not isinstance(m, dict):
                continue
            slug = m.get("slug")
            if not slug:
                continue
            if m.get("closed") is True:
                continue
            merged = dict(m)
            merged.setdefault("eventSlug", e_slug)
            if e_title:
                merged["question"] = e_title
            if e_desc and not (merged.get("description") or "").strip():
                merged["description"] = e_desc
            if e_end:
                merged.setdefault("endDate", e_end)
            rows.append(merged)
    return rows


def _merge_market_rows(flat: list[dict], from_events: list[dict]) -> list[dict]:
    """Overlay /v1/markets prices onto event-child rows; same slug wins merged BBO."""
    by_slug: dict[str, dict] = {}
    for r in from_events:
        s = r.get("slug")
        if s:
            by_slug[s] = dict(r)
    for r in flat:
        s = r.get("slug")
        if not s:
            continue
        if s in by_slug:
            base = dict(by_slug[s])
            q_keep = base.get("question")
            ev_keep = base.get("eventSlug")
            base.update(r)
            if q_keep and not (base.get("question") or "").strip():
                base["question"] = q_keep
            if ev_keep and not (base.get("eventSlug") or "").strip():
                base["eventSlug"] = ev_keep
            by_slug[s] = base
        else:
            by_slug[s] = dict(r)
    return list(by_slug.values())


def _fetch_paginated_market_list(client) -> list[dict]:
    all_rows: list[dict] = []
    offset = 0
    page = 400
    while True:
        resp = client.markets.list(
            {"limit": page, "offset": offset, "active": True, "closed": False}
        )
        rows = _markets_list_response_rows(resp)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page:
            break
        offset += page
        if offset > 4000:
            logger.warning("PolyClient: markets.list pagination stopped at offset %d.", offset)
            break
    return all_rows


def _fetch_paginated_event_market_rows(client) -> list[dict]:
    all_ev: list[dict] = []
    offset = 0
    page = 200
    while True:
        try:
            resp = client.events.list(
                {"limit": page, "offset": offset, "active": True, "closed": False}
            )
        except Exception as exc:
            logger.warning("PolyClient: events.list failed — %s", exc)
            break
        rows = _events_list_response_rows(resp)
        if not rows:
            break
        all_ev.extend(rows)
        if len(rows) < page:
            break
        offset += page
        if offset > 4000:
            logger.warning("PolyClient: events.list pagination stopped at offset %d.", offset)
            break
    return _flatten_event_markets(all_ev)


def _bbo_dict_from_list_row(m: dict) -> dict | None:
    """Use bestBid/bestAsk on the market row when present (Get Markets includes them)."""
    bb = m.get("bestBid") or m.get("bestBidQuote")
    ba = m.get("bestAsk") or m.get("bestAskQuote")
    if bb is None and ba is None:
        return None
    out: dict[str, Any] = {}
    if bb is not None:
        out["bestBid"] = bb if isinstance(bb, dict) else {"value": str(bb)}
    if ba is not None:
        out["bestAsk"] = ba if isinstance(ba, dict) else {"value": str(ba)}
    oi = m.get("openInterest")
    if oi is None:
        oi = m.get("volumeNum") or m.get("volume")
    if oi is not None:
        out["openInterest"] = oi
    return out


def get_weather_markets() -> list[dict]:
    """Fetch active markets and return those that look like daily temperature props."""
    client = get_public_client()
    out: list[dict] = []

    received_at = datetime.now(timezone.utc)
    try:
        flat = _fetch_paginated_market_list(client)
        for m in flat:
            m["received_timestamp"] = received_at
    except Exception as exc:
        logger.error("PolyClient: markets.list failed — %s", exc)
        flat = []

    try:
        event_rows = _fetch_paginated_event_market_rows(client)
        for m in event_rows:
            m["received_timestamp"] = received_at
    except Exception as exc:
        logger.warning("PolyClient: events.list flatten failed — %s", exc)
        event_rows = []

    all_rows = _merge_market_rows(flat, event_rows)
    logger.info(
        "PolyClient: merged %d /v1/markets rows + %d event-child rows → %d unique slugs.",
        len(flat),
        len(event_rows),
        len(all_rows),
    )
    if not all_rows:
        logger.warning(
            "PolyClient: no market rows from API — Polymarket US catalog may differ from polymarket.com."
        )

    temp_title_hits = 0
    priced = 0
    drop_no_strike = 0
    drop_no_price = 0

    for m in all_rows:
        slug = m.get("slug")
        if not slug:
            continue
        display_text = _poly_market_display_text(m)
        if not display_text:
            continue
        outcome = _poly_outcome_text(m)
        probe = f"{display_text} ({outcome})" if outcome else display_text
        if not _looks_like_daily_temp(probe):
            continue
        temp_title_hits += 1

        head = display_text.strip() or _poly_market_display_text(m)
        ot = _poly_outcome_text(m)
        pre_title = f"{head} ({ot})" if ot else head
        if not _poly_strike_fields(pre_title):
            drop_no_strike += 1
            continue

        bbo = _build_price_bbo(client, m, slug)
        bid_g, ask_g = _yes_probs_from_bbo_dict(bbo)
        if bid_g is None and ask_g is None:
            drop_no_price += 1
            continue

        norm = _normalize_market_row(client, m, bbo, display_text=display_text)
        if norm:
            out.append(norm)
            priced += 1

    logger.info(
        "PolyClient: temperature-title candidates=%d, with usable prices=%d "
        "(dropped: no_strike=%d, no_price=%d).",
        temp_title_hits,
        priced,
        drop_no_strike,
        drop_no_price,
    )
    if temp_title_hits and priced == 0:
        logger.warning(
            "PolyClient: 0 priced temperature markets — check BBO/book/lastTrade data for slugs."
        )
    return out


def get_orderbook_as_parsed(slug: str) -> dict | None:
    """Fetch a current executable YES top-of-book snapshot for one market."""
    try:
        patch = _bbo_from_book(get_public_client().markets.book(slug))
    except Exception as exc:
        logger.debug("PolyClient: orderbook refresh %s failed — %s", slug, exc)
        return None
    if not patch:
        return None
    bid, ask = _yes_probs_from_bbo_dict(patch)
    return {
        "ticker": slug,
        "yes_bid": bid,
        "yes_ask": ask,
        "best_bid": bid,
        "best_ask": ask,
        "yes_bid_size": _qty_to_float(patch.get("yes_bid_size")) or 0.0,
        "bid_size": _qty_to_float(patch.get("yes_bid_size")) or 0.0,
        "yes_ask_size": _qty_to_float(patch.get("yes_ask_size")) or 0.0,
        "yes_ask_qty": _qty_to_float(patch.get("yes_ask_qty")) or 0.0,
        "ask_size": _qty_to_float(patch.get("ask_size")) or 0.0,
        "received_timestamp": patch.get("received_timestamp"),
        "orderbook_timestamp": patch.get("orderbook_timestamp"),
        "execution_price_source": "orderbook",
    }


def get_market_result(slug: str) -> str | None:
    """Return 'yes' or 'no' if settled, else None.
    Matches Polymarket US ``MarketSettlement``: ``settlement`` (0 or 1).
    """
    client = get_public_client()
    try:
        raw = client.markets.settlement(slug)
    except Exception as exc:
        logger.debug("PolyClient: settlement(%s) — %s", slug, exc)
        return None

    if isinstance(raw, dict) and "settlement" in raw:
        val = raw["settlement"]
        if val == 1 or val == "1":
            return "yes"
        elif val == 0 or val == "0":
            return "no"
            
    return None


def _execution_summary(response: dict, requested: float, side: str) -> dict:
    """Read confirmed fills; an accepted order ID is never evidence of a fill."""
    executions = []
    seen = set()
    filled = 0.0
    priced_shares = 0.0
    fill_notional = 0.0
    cumulative = 0.0
    snapshot = {}
    state = ""
    malformed = False
    for execution in response.get("executions") or []:
        if not isinstance(execution, dict):
            malformed = True
            continue
        identity = execution.get("id")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        order = execution.get("order") or {}
        if order.get("id") and order["id"] != response.get("id"):
            malformed = True
            continue
        executions.append(execution)
        qty = _qty_to_float(order.get("cumQuantity"))
        if qty is not None and math.isfinite(qty) and qty >= cumulative:
            cumulative = qty
            snapshot = order
        state = order.get("state") or state
        if execution.get("type") not in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"):
            continue
        qty = _qty_to_float(execution.get("lastShares"))
        if qty is None or not math.isfinite(qty):
            malformed = True
            continue
        filled += qty
        px = _amount_to_prob(execution.get("lastPx"))
        if px is not None and math.isfinite(px) and 0 <= px <= 1:
            fill_notional += qty * px
            priced_shares += qty

    # Cumulative quantities include fills not necessarily repeated in this response.
    filled = max(filled, cumulative)
    if filled > requested:
        malformed = True
    average_yes_price = _amount_to_prob(snapshot.get("avgPx"))
    if cumulative != filled or average_yes_price is None or not math.isfinite(average_yes_price):
        average_yes_price = fill_notional / filled if filled > 0 and priced_shares == filled else None
    if average_yes_price is not None and not 0 <= average_yes_price <= 1:
        average_yes_price = None
        malformed = True
    terminal = state in {
        "ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED"
    }
    if state == "ORDER_STATE_FILLED" and filled != requested:
        malformed = True
    remaining = 0.0 if terminal else max(0.0, requested - filled)
    fee = _amount_to_prob(snapshot.get("commissionNotionalTotalCollected"))
    if fee is not None and (not math.isfinite(fee) or fee < 0):
        fee = None
        malformed = True
    return {
        "filled_contracts": filled,
        "remaining_contracts": remaining,
        "requested_contracts": requested,
        "order_status": state or ("filled" if filled == requested else "accepted"),
        "average_fill_price": (
            average_yes_price if side == "yes" or average_yes_price is None else 1.0 - average_yes_price
        ),
        "fees_paid": fee,
        "executions": executions,
        "requires_order_reconciliation": malformed or remaining > 0 or (filled > 0 and (average_yes_price is None or fee is None)),
    }


def place_order(
    market_slug: str,
    side: str,
    quantity: float,
    price_prob: float,
) -> dict:
    """Place a limit order. *price_prob* is the selected side's cost per share.

    ``price`` remains a selected-side limit, never an assumed execution price.
    Actual quantities/prices and pending order metadata come from venue executions.
    """
    side_l = side.lower()
    try:
        quantity = float(quantity)
        price_prob = float(price_prob)
    except (TypeError, ValueError, OverflowError):
        return {"status": "error", "error": "invalid order quantity or price"}
    if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(price_prob) or not 0 < price_prob < 1:
        return {"status": "error", "error": "invalid order quantity or price"}
    side_limit = min(0.99, max(0.01, price_prob + 0.01))
    if side_l == "yes":
        intent = "ORDER_INTENT_BUY_LONG"
        raw_px = side_limit
    elif side_l == "no":
        intent = "ORDER_INTENT_BUY_SHORT"
        # The venue's price is always YES-basis; apply the buffer to NO cost first.
        raw_px = 1.0 - side_limit
    else:
        return {"status": "error", "error": f"bad side {side!r}"}

    px_str = f"{raw_px:.2f}"
    body = {
        "marketSlug": market_slug,
        "intent":     intent,
        "type":       "ORDER_TYPE_LIMIT",
        "price":      {"value": px_str, "currency": "USD"},
        "quantity":   int(quantity) if quantity.is_integer() else quantity,
        "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        "synchronousExecution": True,
    }
    client = get_client()
    try:
        resp = client.orders.create(body)
    except Exception as exc:
        logger.exception("PolyClient: order failed — %s", exc)
        return {"status": "error", "error": str(exc)}

    oid = resp.get("id") or ""
    if not oid:
        return {"status": "error", "error": "no order id in response", "raw": resp}

    return {
        "order_id":          oid,
        "status":            "ok",
        "price":             float(px_str) if side_l == "yes" else round(1.0 - float(px_str), 2),
        "yes_price":         float(px_str),
        "time_in_force":     body["tif"],
        **_execution_summary(resp, quantity, side_l),
    }
