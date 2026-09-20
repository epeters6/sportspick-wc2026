"""Final-only venue labels with persistent public response caching and paced reads."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from . import SOURCE
from .dataset import finite, utc


def final_result(venue, market_id, market):
    if venue == "kalshi":
        if market.get("ticker") != market_id or market.get("status") not in ("settled", "finalized") or market.get("result") not in ("yes", "no"):
            return None
        value = market.get("settlement_value_dollars")
        if value is None and market.get("settlement_value") is not None:
            value = finite(market["settlement_value"])
            value = value / 100 if value is not None else None
        if isinstance(value, bool) or finite(value) not in (0, 1):
            return None
        expected = 1 if market["result"] == "yes" else 0
        return market["result"] if float(value) == expected else None
    if venue == "polymarket":
        value = market.get("settlement")
        if market.get("slug") != market_id or isinstance(value, bool) or value not in (0, 1, "0", "1"):
            return None
        return "yes" if value in (1, "1") else "no"
    return None


def label_pending(db, feeds, *, max_markets=100, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    # Retrieve older target dates only. Pending current-day markets never occupy
    # the front of the resolution queue; all snapshots of a market update together.
    pending = []
    offset = 0
    while True:
        page = db.table("model_predictions").select("id,outcome,metadata")\
            .eq("source", SOURCE).is_("resolved_at", "null").lt("metadata->>target_date", now.date().isoformat())\
            .order("created_at").order("id").range(offset, offset + 499).execute().data or []
        pending.extend(page)
        if len(page) < 500:
            break
        offset += 500
    groups = defaultdict(list)
    for row in pending:
        if utc(row["metadata"]["contract"]["window_end"]) < now:
            groups[(row["metadata"]["platform"], row["outcome"])].append(row)
    report = {"pending_rows": len(pending), "markets_checked": 0, "rows_labelled": 0, "errors": []}
    # Rotate unresolved IDs so disputed or unavailable old markets cannot starve
    # later contracts of official labels indefinitely.
    cursor_key = SOURCE + "_label_cursor"
    saved = db.table("app_settings").select("value").eq("key", cursor_key).execute().data or []
    last = tuple(saved[0]["value"].get("last", [])) if saved else ()
    keys = sorted(groups)
    keys = ([key for key in keys if key > last] + [key for key in keys if key <= last])[:max_markets]
    for venue, market_id in keys:
        rows = groups[(venue, market_id)]
        try:
            if venue == "kalshi":
                response = feeds.fetch("https://external-api.kalshi.com/trade-api/v2/markets/" + market_id, {}, ttl=300)
                market = response["data"].get("market", {})
                report["markets_checked"] += 1
                winner = final_result(venue, market_id, market)
                if winner is None:
                    continue
                settled = market.get("settlement_ts") or market.get("settled_time")
                rules = "\n".join(str(market.get(k) or "") for k in ("rules_primary", "rules_secondary", "description", "resolution_source")).strip()
                if any(rules != row["metadata"]["contract"]["rules_text"] for row in rows):
                    raise ValueError("FINAL_RULES_DIFFER_FROM_CAPTURE")
            elif venue == "polymarket":
                response = feeds.fetch("https://gateway.polymarket.us/v1/markets/" + market_id + "/settlement", {}, ttl=300)
                market = response["data"]
                report["markets_checked"] += 1
                winner = final_result(venue, market_id, market)
                if winner is None:
                    continue
                settled = None
            else:
                raise ValueError("UNSUPPORTED_VENUE")
            available = clock().isoformat()
            for row in rows:
                meta = {**row["metadata"], "label_source": "venue_official", "official_market_id": market_id,
                        "official_result": winner, "official_settled_at": settled,
                        "label_available_at": available, "official_response_hash": response["content_hash"]}
                db.table("model_predictions").update({"resolved_at": available, "is_correct": winner == "yes", "metadata": meta})\
                    .eq("id", row["id"]).eq("source", SOURCE).is_("resolved_at", "null").execute()
                report["rows_labelled"] += 1
        except Exception as exc:
            report["errors"].append({"venue": venue, "market": market_id,
                                     "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    if keys:
        db.table("app_settings").upsert({"key": cursor_key, "value": {"last": list(keys[-1])}}, on_conflict="key").execute()
    return report
