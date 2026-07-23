"""Audit and safely reconcile historical match-linked MLB autobets."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db import get_db
from backend.sports_data.bet_settlement import pick_won_for_autobet
from backend.trading.settlement_integrity import (
    SETTLEMENT_VERSION,
    expected_autobet_pnl,
    parse_timestamp,
)


def fetch_linked_mlb_settlements(db) -> list[dict]:
    return (
        db.table("autobets")
        .select(
            "id, match_id, sport, outcome_name, bet_type, bet_line, bet_subject, "
            "status, pnl, stake, shares, market_price, resolved_at, "
            "settlement_version, settlement_match_id, settlement_corrected_at, "
            "matches:matches!autobets_match_id_fkey("
            "id, sport, external_id, home_team, away_team, scheduled_at, "
            "finished_at, winner, is_final, home_score, away_score, match_stats)"
        )
        .in_("status", ["won", "lost"])
        .not_.is_("match_id", "null")
        .ilike("sport", "mlb%")
        .execute()
        .data
        or []
    )


def _one_match(row: dict) -> dict | None:
    match = row.get("matches")
    if isinstance(match, list):
        return match[0] if len(match) == 1 else None
    return match if isinstance(match, dict) else None


def analyze_settlements(rows: list[dict], *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    proposed: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    status_pnl_corrections = premature_count = attention_count = consistent = 0

    for row in rows:
        match = _one_match(row)
        reasons: list[str] = []
        if not match or str(row.get("match_id")) != str(match.get("id")):
            incomplete.append({"id": row.get("id"), "reason": "EXACT_MATCH_NOT_FOUND"})
            continue
        scheduled_at = parse_timestamp(match.get("scheduled_at"))
        if scheduled_at is None:
            incomplete.append({"id": row.get("id"), "reason": "SETTLEMENT_DATA_INCOMPLETE"})
            continue
        if match.get("is_final") is not True:
            incomplete.append({"id": row.get("id"), "reason": "EXACT_MATCH_NOT_FINAL"})
            continue
        if now < scheduled_at:
            incomplete.append({"id": row.get("id"), "reason": "PRESTART_SETTLEMENT_BLOCK"})
            continue

        won = pick_won_for_autobet(
            bet_type=row.get("bet_type") or "moneyline",
            outcome_name=row.get("outcome_name") or "",
            bet_line=row.get("bet_line"),
            bet_subject=row.get("bet_subject"),
            match=match,
            match_stats=match.get("match_stats"),
        )
        if won is None:
            incomplete.append({"id": row.get("id"), "reason": "SETTLEMENT_DATA_INCOMPLETE"})
            continue

        expected_status = "won" if won else "lost"
        expected_pnl = expected_autobet_pnl(
            won=won,
            stake=float(row.get("stake") or 0.0),
            shares=float(row.get("shares") or 0.0),
            market_price=float(row.get("market_price") or 0.0),
        )
        wrong_status = row.get("status") != expected_status
        try:
            wrong_pnl = abs(float(row.get("pnl")) - expected_pnl) > 0.0100001
        except (TypeError, ValueError):
            wrong_pnl = True
        premature = False
        resolved_at = parse_timestamp(row.get("resolved_at"))
        if resolved_at is not None and resolved_at < scheduled_at:
            corrected_at = parse_timestamp(row.get("settlement_corrected_at"))
            historical_reverified = (
                row.get("settlement_version") == SETTLEMENT_VERSION
                and str(row.get("settlement_match_id")) == str(row.get("match_id"))
                and corrected_at is not None
                and corrected_at >= scheduled_at
            )
            premature = not historical_reverified

        if wrong_status:
            reasons.append("WRONG_STATUS")
        if wrong_pnl:
            reasons.append("WRONG_PNL")
        if premature:
            reasons.append("PREMATURE_SETTLEMENT")
        correction_needed = wrong_status or wrong_pnl
        requires_attention = correction_needed or premature
        if correction_needed:
            status_pnl_corrections += 1
        if premature:
            premature_count += 1
        if requires_attention:
            attention_count += 1
        else:
            consistent += 1
            reasons.append("REVERIFIED_EXACT_MATCH")

        proposed.append(
            {
                "autobet_id": row.get("id"),
                "match_id": row.get("match_id"),
                "external_id": match.get("external_id"),
                "scheduled_at": match.get("scheduled_at"),
                "winner": match.get("winner"),
                "prior_status": row.get("status"),
                "corrected_status": expected_status,
                "prior_pnl": row.get("pnl"),
                "corrected_pnl": expected_pnl,
                "wrong_status": wrong_status,
                "wrong_pnl": wrong_pnl,
                "premature": premature,
                "requires_attention": requires_attention,
                "reason": ",".join(reasons),
            }
        )

    return {
        "linked": len(rows),
        "status_pnl_corrections": status_pnl_corrections,
        "premature": premature_count,
        "reconciliation_rows": attention_count,
        "already_consistent": consistent,
        "incomplete": incomplete,
        "proposed": proposed,
        "reconciled_pnl": round(
            sum(float(item["corrected_pnl"]) for item in proposed), 2
        ),
    }


def _assert_expected(args: argparse.Namespace, result: dict) -> None:
    expected = {
        "linked": args.expected_linked,
        "status_pnl_corrections": args.expected_status_mismatches,
        "reconciliation_rows": args.expected_reconciliation_rows,
    }
    missing = [name for name, value in expected.items() if value is None]
    if missing:
        raise RuntimeError(
            "--apply requires all expected-count locks: " + ", ".join(missing)
        )
    mismatches = [
        f"{name}: observed={result[name]} expected={value}"
        for name, value in expected.items()
        if result[name] != value
    ]
    if mismatches:
        raise RuntimeError("Expected-count lock failed: " + "; ".join(mismatches))


def apply_reconciliation(db, result: dict) -> int:
    if result["incomplete"]:
        raise RuntimeError(
            f"Cannot apply with {len(result['incomplete'])} incomplete exact-match rows"
        )
    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    for item in result["proposed"]:
        audit = {
            "autobet_id": item["autobet_id"],
            "correction_version": SETTLEMENT_VERSION,
            "action": "corrected" if item["requires_attention"] else "reverified",
            "reason": item["reason"],
            "prior_status": item["prior_status"],
            "corrected_status": item["corrected_status"],
            "prior_pnl": item["prior_pnl"],
            "corrected_pnl": item["corrected_pnl"],
            "match_id": item["match_id"],
            "external_id": item["external_id"],
            "scheduled_at": item["scheduled_at"],
            "winner": item["winner"],
            "details": {
                "wrong_status": item["wrong_status"],
                "wrong_pnl": item["wrong_pnl"],
                "premature": item["premature"],
            },
        }
        existing = (
            db.table("autobet_settlement_audit")
            .select("id, applied_at")
            .eq("autobet_id", item["autobet_id"])
            .eq("correction_version", SETTLEMENT_VERSION)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing and existing[0].get("applied_at"):
            raise RuntimeError(
                f"Audit already applied for autobet {item['autobet_id']}"
            )
        if existing:
            audit_id = existing[0]["id"]
        else:
            inserted = (
                db.table("autobet_settlement_audit").insert(audit).execute().data or []
            )
            if not inserted:
                raise RuntimeError(
                    f"Audit insert returned no row for {item['autobet_id']}"
                )
            audit_id = inserted[0]["id"]

        update = {
            "settlement_version": SETTLEMENT_VERSION,
            "settlement_match_id": item["match_id"],
            "settlement_corrected_at": now,
        }
        if item["wrong_status"] or item["wrong_pnl"]:
            update["status"] = item["corrected_status"]
            update["pnl"] = item["corrected_pnl"]
        db.table("autobets").update(update).eq("id", item["autobet_id"]).execute()
        db.table("autobet_settlement_audit").update(
            {"applied_at": now}
        ).eq("id", audit_id).execute()
        applied += 1
    return applied


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Audit only (default).")
    mode.add_argument("--apply", action="store_true", help="Apply locked corrections.")
    parser.add_argument("--expected-linked", type=int)
    parser.add_argument("--expected-status-mismatches", type=int)
    parser.add_argument("--expected-reconciliation-rows", type=int)
    parser.add_argument("--export", type=Path)
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Exit nonzero when attention or incomplete rows remain.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = get_db()
    result = analyze_settlements(fetch_linked_mlb_settlements(db))
    print(f"Linked settled MLB:              {result['linked']}")
    print(f"Status/P&L corrections:          {result['status_pnl_corrections']}")
    print(f"Premature-settlement rows:       {result['premature']}")
    print(f"Unique rows requiring attention: {result['reconciliation_rows']}")
    print(f"Already consistent:              {result['already_consistent']}")
    print(f"Incomplete exact-match rows:     {len(result['incomplete'])}")
    print(f"Reconciled linked MLB P&L:        ${result['reconciled_pnl']:+.2f}")
    for item in result["proposed"]:
        print("PROPOSED " + json.dumps(item, sort_keys=True, default=str))
    for item in result["incomplete"]:
        print("INCOMPLETE " + json.dumps(item, sort_keys=True, default=str))

    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"Exported: {args.export}")

    if args.apply:
        _assert_expected(args, result)
        applied = apply_reconciliation(db, result)
        print(f"Applied/reverified rows:          {applied}")
    if args.fail_on_mismatch and (
        result["reconciliation_rows"] or result["incomplete"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
