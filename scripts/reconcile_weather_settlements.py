"""Audit or repair historical weather bets from exact official venue results.

Dry-run is the default. Pass ``--apply`` only after reviewing the proposed rows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db import get_db
from backend.trading.settlement_integrity import WEATHER_SETTLEMENT_VERSION
from backend.trading.weather_settlement import (
    _apply_resolution,
    _bet_meta,
    _detect_venue,
    _grade_bet_against_venue,
    check_kalshi_resolution,
    check_polymarket_resolution,
)


def _needs_reconciliation(bet: dict, graded: dict) -> bool:
    try:
        stored_pnl = float(bet.get("pnl"))
    except (TypeError, ValueError):
        return True
    evidence = _bet_meta(bet).get("settlement")
    if not isinstance(evidence, dict):
        return True
    return (
        bet.get("status") != graded["status"]
        or not math.isfinite(stored_pnl)
        or abs(stored_pnl - graded["pnl"]) > 0.000001
        or bet.get("settlement_version") != WEATHER_SETTLEMENT_VERSION
        or any(evidence.get(key) != graded[key] for key in (
            "version", "source", "venue", "market_id", "official_result"
        ))
    )


async def reconcile_weather_settlements(
    *, apply: bool = False, limit: int | None = None, page_size: int = 250,
) -> dict:
    """Audit all rows with stable ID pagination, even if server pages are capped.

    An explicit limit produces a partial audit. Rechecking current-version rows
    detects corrupt evidence and later official corrections without appending
    duplicate history on unchanged rows. No station observations are used.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive or omitted for all rows")
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    db = get_db()
    now = datetime.now(timezone.utc)
    summary = {
        "dry_run": not apply, "reviewed": 0, "officially_resolved": 0,
        "changes": 0, "applied": 0, "unresolved": 0, "errors": 0,
        "unchanged": 0, "complete": False, "proposed": [],
    }
    cursor = None
    results: dict[tuple[str, str], dict | None] = {}
    while limit is None or summary["reviewed"] < limit:
        size = min(page_size, limit - summary["reviewed"]) if limit else page_size
        query = (
            db.table("autobets").select("*")
            .eq("bet_type", "weather").in_("status", ["won", "lost", "void"])
            .lte("created_at", now.isoformat()).order("id").limit(size)
        )
        if cursor is not None:
            query = query.gt("id", cursor)
        rows = query.execute().data or []
        if not rows:
            summary["complete"] = True
            break
        next_cursor = str(rows[-1]["id"])
        if cursor is not None and next_cursor <= cursor:
            raise RuntimeError("Weather reconciliation pagination did not advance")
        cursor = next_cursor
        for bet in rows:
            summary["reviewed"] += 1
            market_id = str(bet.get("market_id") or "")
            venue = str(bet.get("venue") or _detect_venue(market_id)).lower()
            if venue == "polymarket_us":
                venue = "polymarket"
            try:
                key = (venue, market_id)
                if key not in results:
                    if venue == "kalshi":
                        results[key] = await check_kalshi_resolution(market_id)
                    elif venue == "polymarket":
                        results[key] = await check_polymarket_resolution(market_id)
                    else:
                        results[key] = None
                graded = _grade_bet_against_venue(bet, results[key], venue)
            except Exception as exc:
                summary["errors"] += 1
                print(f"ERROR {bet.get('id')} {market_id}: {exc}")
                continue
            if graded is None:
                summary["unresolved"] += 1
                continue
            summary["officially_resolved"] += 1
            if not _needs_reconciliation(bet, graded):
                summary["unchanged"] += 1
                continue
            summary["changes"] += 1
            summary["proposed"].append({
                "id": bet["id"], "market_id": market_id, "venue": venue,
                "prior": {key: bet.get(key) for key in (
                    "status", "pnl", "resolved_at", "settlement_version",
                    "settlement_corrected_at", "metadata",
                )},
                "official": graded,
            })
            print(
                f"{bet['id']} {market_id}: {bet.get('status')}/{bet.get('pnl')} -> "
                f"{graded['status']}/{graded['pnl']} ({graded['official_result']})"
            )
            if apply:
                updated = _apply_resolution(
                    db, bet, graded["status"], graded["pnl"], now,
                    f"reconciled:{venue}", resolution_source=graded["source"],
                    settlement_evidence=graded, correction=True,
                )
                summary["applied" if updated else "errors"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Optional partial-audit cap.")
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--export", type=Path, help="Save complete before/after evidence as JSON.")
    args = parser.parse_args()
    summary = asyncio.run(
        reconcile_weather_settlements(apply=args.apply, limit=args.limit, page_size=args.page_size)
    )
    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    counts = {key: value for key, value in summary.items() if key != "proposed"}
    print(f"weather settlement reconciliation: {counts}")
    if not args.apply and summary["changes"]:
        print("Dry run only; rerun with --apply after reviewing these changes.")
    if summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
