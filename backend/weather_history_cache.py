"""Incremental research history; never advance a cursor after a partial read.

Forecasts are append-only. Official labels update resolved_at, so creation and
resolution cursors both matter. Keep full evidence locally for existing validators.
"""
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def refresh_history(fetch, path, sources, *, since=None, clock=None):
    path = Path(path)
    started = (clock or (lambda: datetime.now(timezone.utc)))()
    scope = {"sources": list(sources), "since": since}
    cached = None
    if path.exists():
        with gzip.open(path, "rt", encoding="utf8") as stream:
            cached = json.load(stream)
        if cached.get("version") != 1 or cached.get("scope") != scope:
            raise ValueError("HISTORY_CACHE_SCOPE_MISMATCH")
    rows = {r["id"]: r for r in cached["rows"]} if cached else {}
    watermark = cached["watermark"] if cached else None
    # A small overlap covers boundary timestamps; ID merge makes retries harmless.
    cursor = (datetime.fromisoformat(watermark) - timedelta(minutes=5)).isoformat() if watermark else None
    downloaded = 0
    for source in sources:
        for field in (("created_at", "resolved_at") if cursor else (None,)):
            for row in fetch(source, field, cursor, since or ((started - timedelta(days=120)).isoformat() if not cached else None)):
                rows[row["id"]] = row
                downloaded += 1
    # Never prune ungraded rows, or records needed by a frozen prospective study.
    result = sorted(rows.values(), key=lambda r: (r["created_at"], str(r["id"])))
    payload = {"version": 1, "scope": scope, "watermark": started.isoformat(), "rows": result}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf8") as stream:
        json.dump(payload, stream, allow_nan=False)
    temporary.replace(path)
    return result, {"cached_rows": len(result), "downloaded_rows": downloaded,
                    "bootstrap": cached is None, "watermark": payload["watermark"]}


def load_cached_history(db, path, sources, *, since=None, page_size=500):
    def fetch(source, field, cursor, lower_bound):
        offset = 0
        while True:
            query = db.table("model_predictions").select("*").eq("source", source)
            if lower_bound:
                query = query.gte("created_at", lower_bound)
            if field:
                query = query.gte(field, cursor)
            page = query.order("created_at").order("id").range(offset, offset + page_size - 1).execute().data or []
            yield from page
            if len(page) < page_size:
                break
            offset += page_size
    return refresh_history(fetch, path, sources, since=since)
