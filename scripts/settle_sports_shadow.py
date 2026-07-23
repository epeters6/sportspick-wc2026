"""Settle Phase 4 MLB shadow obligations by exact match_id and game_pk."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db import get_db
from backend.trading.market_matcher import _canonical

SETTLEMENT_SOURCE = "exact_match_id_game_pk_v1"
SHADOW_SETTLEMENT_MATCH_MISSING = "SHADOW_SETTLEMENT_MATCH_MISSING"
SHADOW_SETTLEMENT_GAME_PK_MISMATCH = "SHADOW_SETTLEMENT_GAME_PK_MISMATCH"
SHADOW_SETTLEMENT_IDENTITY_AMBIGUOUS = "SHADOW_SETTLEMENT_IDENTITY_AMBIGUOUS"
SHADOW_SETTLEMENT_RESULT_INCOMPLETE = "SHADOW_SETTLEMENT_RESULT_INCOMPLETE"


def _canon(value: Any) -> str:
    raw = str(value or "").strip()
    return (_canonical(raw) or raw.lower()).strip()


def _game_pk(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw.startswith("mlb_"):
        raw = raw[4:]
    return raw if raw.isdigit() else None


def _is_mlb_obligation(row: dict) -> bool:
    metadata = row.get("metadata") or {}
    strategy = metadata.get("strategy") if isinstance(metadata, dict) else None
    return (
        str(row.get("candidate_id") or "").startswith("sports_mlb")
        or "mlb" in str(row.get("event_id") or "").lower()
        or str(strategy or "").startswith("mlb_")
    )


def fetch_pending(db) -> list[dict]:
    rows = (
        db.table("clv_obligations")
        .select(
            "candidate_id, event_id, event_start, selected_team, home_team, away_team, "
            "match_id, game_pk, shares, stake, settlement_status, metadata"
        )
        .eq("settlement_status", "pending")
        .execute()
        .data
        or []
    )
    return [row for row in rows if _is_mlb_obligation(row)]


def settle_pending(db=None, *, now: datetime | None = None) -> dict[str, Any]:
    db = db or get_db()
    now = now or datetime.now(timezone.utc)
    obligations = fetch_pending(db)
    match_ids = sorted(
        {str(row["match_id"]) for row in obligations if row.get("match_id")}
    )
    matches = []
    if match_ids:
        matches = (
            db.table("matches")
            .select(
                "id, external_id, home_team, away_team, scheduled_at, winner, "
                "is_final, home_score, away_score"
            )
            .in_("id", match_ids)
            .execute()
            .data
            or []
        )
    match_map = {str(match["id"]): match for match in matches}
    summary: dict[str, Any] = {
        "pending": len(obligations),
        "settled": 0,
        "won": 0,
        "lost": 0,
        "failures": {},
    }

    def fail(reason: str, candidate_id: Any) -> None:
        failures = summary["failures"]
        failures[reason] = failures.get(reason, 0) + 1
        print(f"{reason} candidate_id={candidate_id}")

    for row in obligations:
        candidate_id = row.get("candidate_id")
        match_id = row.get("match_id")
        match = match_map.get(str(match_id)) if match_id else None
        if not match:
            fail(SHADOW_SETTLEMENT_MATCH_MISSING, candidate_id)
            continue
        stored_pk = _game_pk(row.get("game_pk"))
        match_pk = _game_pk(match.get("external_id"))
        if not stored_pk or not match_pk or stored_pk != match_pk:
            fail(SHADOW_SETTLEMENT_GAME_PK_MISMATCH, candidate_id)
            continue
        if match.get("is_final") is not True:
            fail(SHADOW_SETTLEMENT_RESULT_INCOMPLETE, candidate_id)
            continue

        selected = _canon(row.get("selected_team"))
        home = _canon(match.get("home_team"))
        away = _canon(match.get("away_team"))
        if not selected or selected not in {home, away} or home == away:
            fail(SHADOW_SETTLEMENT_IDENTITY_AMBIGUOUS, candidate_id)
            continue
        winner = _canon(match.get("winner"))
        if not winner or winner not in {home, away}:
            fail(SHADOW_SETTLEMENT_RESULT_INCOMPLETE, candidate_id)
            continue
        try:
            shares = float(row.get("shares"))
            stake = float(row.get("stake"))
        except (TypeError, ValueError):
            fail(SHADOW_SETTLEMENT_RESULT_INCOMPLETE, candidate_id)
            continue

        won = selected == winner
        status = "won" if won else "lost"
        pnl = round(shares - stake, 2) if won else round(-stake, 2)
        db.table("clv_obligations").update(
            {
                "settlement_status": status,
                "settlement_result": won,
                "settlement_pnl": pnl,
                "settled_at": now.isoformat(),
                "settlement_source": SETTLEMENT_SOURCE,
            }
        ).eq("candidate_id", candidate_id).execute()
        summary["settled"] += 1
        summary[status] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one settlement pass.")
    parser.parse_args(argv)
    summary = settle_pending()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
