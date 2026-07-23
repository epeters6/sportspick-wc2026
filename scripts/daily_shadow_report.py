"""Previous-ET-day settlement integrity report for Discord."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from loguru import logger

from backend.config import get_settings
from backend.db import get_db
from backend.trading.autobet_learning import (
    assess_live_readiness,
    settlement_integrity_datasets,
)

REPORT_TZ = ZoneInfo("America/New_York")

STRATEGY_LABELS = {
    "legacy_consensus_mlb": "Legacy MLB — verified only",
    "phase4_mlb_moneyline": "Phase 4 MLB moneyline — exact shadow outcomes",
    "weather_high": "Weather high — verified only",
    "weather_low": "Weather low — verified only",
    "legacy_consensus_football": "Football — verified only",
}


def previous_local_day(now_utc: datetime) -> tuple[date, datetime, datetime]:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(REPORT_TZ)
    report_date = local_now.date() - timedelta(days=1)
    start_local = datetime.combine(report_date, time.min, tzinfo=REPORT_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        report_date,
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )


def _strategy(row: dict) -> str:
    if row.get("strategy"):
        return str(row["strategy"])
    sport = str(row.get("sport") or "").lower()
    metadata = row.get("metadata") or {}
    if sport == "weather":
        metric = metadata.get("metric", "high") if isinstance(metadata, dict) else "high"
        return f"weather_{metric}"
    if "mlb" in sport or "baseball" in sport:
        return "legacy_consensus_mlb"
    if "football" in sport or "soccer" in sport:
        return "legacy_consensus_football"
    return "legacy_other"


def _event_date(row: dict) -> str | None:
    value = row.get("event_date")
    return value.isoformat() if isinstance(value, date) else str(value)[:10] if value else None


def _agg(rows: list[dict], *, excluded: int = 0) -> dict[str, Any]:
    wins = sum(1 for row in rows if row.get("status") == "won")
    losses = sum(1 for row in rows if row.get("status") == "lost")
    staked = sum(float(row.get("stake") or 0.0) for row in rows)
    pnl = sum(float(row.get("pnl") or 0.0) for row in rows)
    return {
        "verified": len(rows),
        "excluded": excluded,
        "wins": wins,
        "losses": losses,
        "staked": staked,
        "pnl": pnl,
        "roi_pct": pnl / staked * 100.0 if staked else None,
    }


def _fmt_line(label: str, aggregate: dict[str, Any], *, phase: str) -> str:
    roi = (
        f"{aggregate['roi_pct']:+.1f}%"
        if aggregate["roi_pct"] is not None
        else "n/a"
    )
    return (
        f"**{label}** ({phase})\n"
        f"{aggregate['wins']}-{aggregate['losses']} · "
        f"verified `{aggregate['verified']}` · excluded `{aggregate['excluded']}` · "
        f"risked `${aggregate['staked']:.2f}` · "
        f"P&L `${aggregate['pnl']:+.2f}` · ROI `{roi}`"
    )


def _fetch_phase4_rows(
    db,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict]:
    try:
        rows = (
            db.table("clv_obligations")
            .select(
                "candidate_id, event_start, settlement_status, settlement_result, "
                "settlement_pnl, stake, model_prob, market_prob, settlement_source"
            )
            .in_("settlement_status", ["won", "lost"])
            .gte("event_start", start_utc.isoformat())
            .lt("event_start", end_utc.isoformat())
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.warning("Phase 4 outcome fetch failed: {}", exc)
        return []
    return [
        {
            **row,
            "status": row.get("settlement_status"),
            "pnl": row.get("settlement_pnl"),
            "strategy": "phase4_mlb_moneyline",
        }
        for row in rows
    ]


def build_report(
    *,
    now_utc: datetime | None = None,
    db=None,
    refresh_guardian: bool = True,
) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    report_date, start_utc, end_utc = previous_local_day(now_utc)
    db = db or get_db()
    datasets = settlement_integrity_datasets(db)
    verified = [
        row
        for row in datasets["verified_rows"]
        if _event_date(row) == report_date.isoformat()
    ]
    excluded = [
        row
        for row in datasets["invalid_rows"] + datasets["unverifiable_rows"]
        if _event_date(row) == report_date.isoformat()
    ]

    by_strategy: dict[str, list[dict]] = {
        name: [] for name in STRATEGY_LABELS
    }
    for row in verified:
        strategy = _strategy(row)
        if strategy in by_strategy:
            by_strategy[strategy].append(row)
    by_strategy["phase4_mlb_moneyline"] = _fetch_phase4_rows(
        db, start_utc, end_utc
    )

    excluded_by_strategy: dict[str, int] = {
        name: 0 for name in STRATEGY_LABELS
    }
    exclusion_reasons: dict[str, int] = {}
    for row in excluded:
        strategy = _strategy(row)
        if strategy in excluded_by_strategy:
            excluded_by_strategy[strategy] += 1
        reason = str(row.get("_integrity_reason") or "UNKNOWN")
        exclusion_reasons[reason] = exclusion_reasons.get(reason, 0) + 1

    try:
        open_count = len(
            db.table("autobets")
            .select("id")
            .eq("status", "open")
            .execute()
            .data
            or []
        )
    except Exception:
        open_count = 0

    guardian = {"halted": True, "reasons": ["GUARDIAN_STATUS_UNAVAILABLE"]}
    if refresh_guardian:
        try:
            from scripts.guardian_health import check_health

            guardian = check_health(emit=False)
        except Exception as exc:
            logger.warning("Guardian refresh failed: {}", exc)

    try:
        from backend.trading.live_toggle import is_live_mode

        live_enabled = bool(is_live_mode())
    except Exception:
        live_enabled = False

    return {
        "report_date": report_date,
        "window_start_utc": start_utc,
        "window_end_utc": end_utc,
        "by_strategy": by_strategy,
        "excluded_by_strategy": excluded_by_strategy,
        "integrity_excluded_count": len(excluded),
        "integrity_exclusion_reasons": exclusion_reasons,
        "open_count": open_count,
        "guardian": guardian,
        "live_enabled": live_enabled,
        "readiness": assess_live_readiness(db),
    }


def build_embed(report: dict[str, Any]) -> dict[str, Any]:
    report_date = report["report_date"]
    start_utc = report["window_start_utc"]
    end_utc = report["window_end_utc"]
    lines = [
        f"Report date: **{report_date.isoformat()} ET**",
        (
            f"Window: `{start_utc.isoformat()}` to `{end_utc.isoformat()}` "
            "(previous ET calendar/event day)"
        ),
        "",
    ]
    for strategy in (
        "legacy_consensus_mlb",
        "phase4_mlb_moneyline",
        "weather_high",
        "weather_low",
        "legacy_consensus_football",
    ):
        phase = "Phase 4" if strategy == "phase4_mlb_moneyline" else "legacy"
        lines.append(
            _fmt_line(
                STRATEGY_LABELS[strategy],
                _agg(
                    report["by_strategy"].get(strategy, []),
                    excluded=report["excluded_by_strategy"].get(strategy, 0),
                ),
                phase=phase,
            )
        )
        lines.append("")

    reasons = report.get("integrity_exclusion_reasons") or {}
    reason_text = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())) or "none"
    lines.extend(
        [
            f"Integrity exclusions: **{report['integrity_excluded_count']}** ({reason_text})",
            f"Open positions: **{report['open_count']}**",
            "Guardian status: **HALTED**"
            if report["guardian"].get("halted")
            else "Guardian status: **clear**",
            "Live trading status: **ON**"
            if report["live_enabled"]
            else "Live trading status: **OFF**",
            "",
            "Legacy readiness calculation only: "
            + report["readiness"].get("message", "unavailable"),
            "Phase 4 live remains blocked until sports validation gates pass.",
        ]
    )
    if report["guardian"].get("reasons"):
        lines.append(
            "Guardian reasons: " + "; ".join(report["guardian"].get("reasons") or [])
        )

    total_pnl = sum(
        _agg(rows)["pnl"] for rows in report["by_strategy"].values()
    )
    color = 0x2ECC71 if total_pnl > 0 else 0xE74C3C if total_pnl < 0 else 0x95A5A6
    return {
        "title": "Daily Settlement Integrity Report",
        "description": "\n".join(lines),
        "color": color,
        "footer": {
            "text": "Legacy and Phase 4 results are intentionally separated"
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_report(*, print_only: bool = False) -> bool:
    embed = build_embed(build_report())
    if print_only:
        print(json.dumps(embed, indent=2))
        return True

    webhook_url = get_settings().discord_webhook_url
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set — printing report instead")
        print(json.dumps(embed, indent=2))
        return False
    response = httpx.post(
        webhook_url,
        json={"username": "SportsPick Daily Report", "embeds": [embed]},
        timeout=15,
    )
    if response.status_code in (200, 204):
        logger.info("Daily settlement integrity report sent to Discord")
        return True
    logger.error(
        "Discord webhook returned {}: {}",
        response.status_code,
        response.text[:200],
    )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-only", action="store_true")
    args = parser.parse_args(argv)
    return 0 if send_report(print_only=args.print_only) else 1


if __name__ == "__main__":
    raise SystemExit(main())
