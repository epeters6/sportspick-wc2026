"""
Daily shadow-betting results report → Discord.

Summarizes the past 24h of settled autobets (paper AND live) per domain
(MLB / World Cup football / weather), plus cumulative track record and the
live-promotion gate status, and posts one Discord embed.

Runs daily from GitHub Actions (see .github/workflows/daily_report.yml).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from loguru import logger

from backend.config import get_settings
from backend.db import get_db

DOMAIN_LABELS = {
    "mlb": "⚾ MLB",
    "football": "⚽ World Cup",
    "weather": "🌡️ Weather",
    "other": "🎲 Other",
}


def _domain(sport: str | None) -> str:
    s = (sport or "").lower()
    if "mlb" in s or "baseball" in s:
        return "mlb"
    if "football" in s or "soccer" in s:
        return "football"
    if "weather" in s:
        return "weather"
    return "other"


def _agg(rows: list[dict]) -> dict[str, Any]:
    wins = sum(1 for r in rows if r.get("status") == "won")
    staked = sum(float(r.get("stake") or 0.0) for r in rows)
    pnl = sum(float(r.get("pnl") or 0.0) for r in rows)
    clv_vals = [float(r["clv"]) for r in rows if r.get("clv") is not None]
    return {
        "n": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "staked": staked,
        "pnl": pnl,
        "roi_pct": (pnl / staked * 100.0) if staked > 0 else 0.0,
        "avg_clv": (sum(clv_vals) / len(clv_vals)) if clv_vals else None,
    }


def _fmt_line(label: str, a: dict[str, Any]) -> str:
    if a["n"] == 0:
        return f"{label}: no settled bets"
    clv = f" · CLV `{a['avg_clv']:+.3f}`" if a["avg_clv"] is not None else ""
    return (
        f"{label}: **{a['wins']}-{a['losses']}** · "
        f"P&L `${a['pnl']:+.2f}` · ROI `{a['roi_pct']:+.1f}%`{clv}"
    )


def build_report() -> dict[str, Any]:
    db = get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    settled_24h = (
        db.table("autobets")
        .select("sport, mode, status, stake, pnl, clv, resolved_at")
        .in_("status", ["won", "lost"])
        .gte("resolved_at", since)
        .execute()
        .data or []
    )
    all_settled = (
        db.table("autobets")
        .select("sport, mode, status, stake, pnl, clv")
        .in_("status", ["won", "lost"])
        .execute()
        .data or []
    )
    open_bets = (
        db.table("autobets")
        .select("id, mode")
        .eq("status", "open")
        .execute()
        .data or []
    )

    by_domain_24h: dict[str, list[dict]] = {}
    for r in settled_24h:
        by_domain_24h.setdefault(_domain(r.get("sport")), []).append(r)

    live_24h = [r for r in settled_24h if (r.get("mode") or "paper") == "live"]

    # Promotion gate + guardian status
    readiness: dict[str, Any] = {}
    try:
        from backend.trading.autobet_learning import assess_live_readiness
        readiness = assess_live_readiness(db)
    except Exception as exc:
        logger.warning(f"Readiness check failed: {exc}")

    # Refresh guardian state — the committed halt file is stale on fresh CI checkouts.
    guardian = {"halted": False, "reasons": []}
    try:
        from scripts.guardian_health import check_health
        check_health()
    except Exception as exc:
        logger.warning(f"Guardian refresh failed: {exc}")
    halt_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".guardian_halt.json")
    try:
        import json
        with open(halt_file) as f:
            guardian = json.load(f)
    except Exception:
        pass

    return {
        "settled_24h": settled_24h,
        "by_domain_24h": by_domain_24h,
        "live_24h": live_24h,
        "all_settled": all_settled,
        "open_count": len(open_bets),
        "readiness": readiness,
        "guardian": guardian,
    }


def build_embed(report: dict[str, Any]) -> dict[str, Any]:
    day_agg = _agg(report["settled_24h"])
    cum_agg = _agg(report["all_settled"])
    readiness = report["readiness"]
    guardian = report["guardian"]

    lines = [_fmt_line("**Yesterday (all domains)**", day_agg), ""]
    for key in ("mlb", "football", "weather", "other"):
        rows = report["by_domain_24h"].get(key)
        if rows:
            lines.append(_fmt_line(DOMAIN_LABELS[key], _agg(rows)))
    if report["live_24h"]:
        lines.append("")
        lines.append(_fmt_line("💸 **LIVE bets**", _agg(report["live_24h"])))

    lines.append("")
    lines.append(_fmt_line("📈 **Cumulative (shadow track record)**", cum_agg))
    lines.append(f"Open positions: **{report['open_count']}**")

    # Promotion gate
    if readiness:
        if readiness.get("live_ready"):
            gate = "🟢 **READY FOR LIVE** — shadow record has proven effective."
            try:
                from backend.trading.live_toggle import is_live_mode
                live_now = is_live_mode()
            except Exception:
                live_now = False
            if not live_now:
                gate += " Flip the Go Live toggle on the dashboard to start."
        else:
            gate = f"🟡 Shadow validation in progress — {readiness.get('message', '')}"
        lines.append("")
        lines.append(gate)

    if guardian.get("halted"):
        lines.append("🔴 **GUARDIAN HALT ACTIVE**: " + "; ".join(guardian.get("reasons") or ["unknown"]))

    day_pnl = day_agg["pnl"]
    color = 0x2ECC71 if day_pnl > 0 else (0xE74C3C if day_pnl < 0 else 0x95A5A6)

    return {
        "title": "📊 Daily Shadow Betting Report",
        "description": "\n".join(lines),
        "color": color,
        "footer": {"text": "SportsPick Quant • paper results until promotion gate passes"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_report() -> bool:
    settings = get_settings()
    webhook_url = settings.discord_webhook_url
    report = build_report()
    embed = build_embed(report)

    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set — printing report instead")
        print(embed["description"])
        return False

    payload = {
        "username": "SportsPick Daily Report",
        "embeds": [embed],
    }
    resp = httpx.post(webhook_url, json=payload, timeout=15)
    if resp.status_code in (200, 204):
        logger.info("Daily shadow report sent to Discord")
        return True
    logger.error(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
    return False


if __name__ == "__main__":
    send_report()
