"""Audit or repair historical weather bets from exact official venue results.

Dry-run is the default. Pass ``--apply`` only after reviewing the proposed rows.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db import get_db
from backend.trading.settlement_integrity import WEATHER_SETTLEMENT_VERSION
from backend.trading.weather_settlement import (
    _apply_resolution,
    _detect_venue,
    _grade_bet_against_venue,
    check_kalshi_resolution,
    check_polymarket_resolution,
)


async def reconcile_weather_settlements(*, apply: bool, limit: int) -> dict:
    db = get_db()
    rows = (
        db.table("autobets")
        .select("*")
        .eq("bet_type", "weather")
        .in_("status", ["won", "lost", "void"])
        .order("created_at")
        .limit(max(1, int(limit)))
        .execute()
        .data
        or []
    )
    summary = {
        "reviewed": 0,
        "officially_resolved": 0,
        "changes": 0,
        "applied": 0,
        "unresolved": 0,
        "errors": 0,
    }
    now = datetime.now(timezone.utc)
    for bet in rows:
        if bet.get("settlement_version") == WEATHER_SETTLEMENT_VERSION:
            continue
        summary["reviewed"] += 1
        market_id = str(bet.get("market_id") or "")
        venue = str(bet.get("venue") or _detect_venue(market_id)).lower()
        try:
            if venue == "kalshi":
                result = await check_kalshi_resolution(market_id)
            else:
                result = await check_polymarket_resolution(market_id)
            graded = _grade_bet_against_venue(bet, result, venue)
        except Exception as exc:
            summary["errors"] += 1
            print(f"ERROR {bet.get('id')} {market_id}: {exc}")
            continue
        if graded is None:
            summary["unresolved"] += 1
            continue
        summary["officially_resolved"] += 1
        changed = (
            str(bet.get("status") or "") != graded["status"]
            or abs(float(bet.get("pnl") or 0.0) - float(graded["pnl"])) > 0.01
            or bet.get("settlement_version") != WEATHER_SETTLEMENT_VERSION
        )
        if not changed:
            continue
        summary["changes"] += 1
        print(
            f"{bet.get('id')} {market_id}: "
            f"{bet.get('status')}/{bet.get('pnl')} -> "
            f"{graded['status']}/{graded['pnl']} ({graded['official_result']})"
        )
        if apply and _apply_resolution(
            db,
            bet,
            graded["status"],
            graded["pnl"],
            now,
            f"reconciled:{venue}",
            resolution_source=graded["source"],
            settlement_evidence=graded,
            correction=True,
        ):
            summary["applied"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    summary = asyncio.run(
        reconcile_weather_settlements(apply=args.apply, limit=args.limit)
    )
    print(f"weather settlement reconciliation: {summary}")
    if not args.apply and summary["changes"]:
        print("Dry run only; rerun with --apply after reviewing these changes.")


if __name__ == "__main__":
    main()
