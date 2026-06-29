"""
Discord webhook alerts for high-confidence consensus picks.

Sends a rich embed message to a Discord channel when:
  1. A new consensus with confidence >= ALERT_THRESHOLD forms.
  2. The match is within the next ALERT_HOURS hours.
  3. The alert hasn't been sent already (tracked in Supabase).

Set DISCORD_WEBHOOK_URL in your .env to enable.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from loguru import logger

from backend.config import get_settings

ALERT_THRESHOLD = 0.70       # minimum consensus confidence to alert on
ALERT_HOURS = 48             # only alert for matches within this many hours
COLOUR_MAP = {               # embed colour by confidence level
    "high": 0x2ECC71,        # green  (≥ 0.80)
    "medium": 0xF39C12,      # orange (≥ 0.70)
}


def _embed_colour(confidence: float) -> int:
    return COLOUR_MAP["high"] if confidence >= 0.80 else COLOUR_MAP["medium"]


def _format_pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def _build_embed(consensus: dict, match: dict) -> dict:
    """Build a Discord embed payload for one consensus pick."""
    home = match.get("home_team", "?")
    away = match.get("away_team", "?")
    stage = match.get("stage", "")
    scheduled = match.get("scheduled_at", "")
    winner = consensus.get("predicted_winner", "?")
    conf = consensus.get("confidence", 0.0)
    pick_count = consensus.get("pick_count", 0)

    home_prob = consensus.get("home_probability", 0.0)
    draw_prob = consensus.get("draw_probability", 0.0)
    away_prob = consensus.get("away_probability", 0.0)

    try:
        match_dt = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        time_str = match_dt.strftime("%a %b %d · %H:%M UTC")
    except Exception:
        time_str = scheduled or "TBD"

    prob_bar = (
        f"🏠 {home}: **{_format_pct(home_prob)}**  "
        f"🤝 Draw: **{_format_pct(draw_prob)}**  "
        f"✈️ {away}: **{_format_pct(away_prob)}**"
    )

    conf_emoji = "🔒" if conf >= 0.80 else "📊"

    return {
        "title": f"{conf_emoji} {home} vs {away}",
        "description": (
            f"**Consensus: {winner}** wins\n"
            f"{prob_bar}"
        ),
        "color": _embed_colour(conf),
        "fields": [
            {"name": "Confidence", "value": _format_pct(conf), "inline": True},
            {"name": "Pickers", "value": str(pick_count), "inline": True},
            {"name": "Stage", "value": stage or "Group Stage", "inline": True},
            {"name": "Kick-off", "value": time_str, "inline": False},
        ],
        "footer": {"text": "SportsPick Consensus Alert • picks may not reflect final odds"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def send_consensus_alerts() -> int:
    """
    Check for high-confidence consensus picks for upcoming matches and post
    Discord embeds for any that haven't been alerted yet.
    Returns the number of alerts sent.
    """
    settings = get_settings()
    webhook_url = getattr(settings, "discord_webhook_url", None)
    if not webhook_url:
        logger.debug("DISCORD_WEBHOOK_URL not set — skipping alerts")
        return 0

    from backend.db import get_db
    db = get_db()

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=ALERT_HOURS)

    # Find high-confidence consensus picks for upcoming matches
    rows = (
        db.table("consensus_picks")
        .select("*, matches(id, home_team, away_team, scheduled_at, stage, is_final)")
        .gte("confidence", ALERT_THRESHOLD)
        .execute()
        .data or []
    )

    alerts_sent = 0

    async with httpx.AsyncClient(timeout=10) as client:
        for row in rows:
            match = row.get("matches") or {}
            if match.get("is_final"):
                continue

            scheduled_str = match.get("scheduled_at", "")
            try:
                match_dt = datetime.fromisoformat(scheduled_str.replace("Z", "+00:00"))
            except Exception:
                continue

            # Only alert for matches within our window, not already past
            if not (now <= match_dt <= cutoff):
                continue

            # Check if we already sent an alert for this consensus (use a simple
            # key stored in the consensus_picks table via the "alerted_at" column —
            # we skip if the column doesn't exist and just send every run instead)
            alerted_at = row.get("alerted_at")
            if alerted_at:
                continue

            embed = _build_embed(row, match)
            payload = {
                "username": "SportsPick Bot",
                "avatar_url": "https://em-content.zobj.net/source/apple/391/soccer-ball_26bd.png",
                "embeds": [embed],
            }

            try:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code in (200, 204):
                    alerts_sent += 1
                    logger.info(
                        f"Discord alert sent: {match.get('home_team')} vs "
                        f"{match.get('away_team')} → {row.get('predicted_winner')} "
                        f"({row.get('confidence', 0)*100:.0f}%)"
                    )
                    # Mark as alerted if the column exists
                    try:
                        db.table("consensus_picks").update(
                            {"alerted_at": now.isoformat()}
                        ).eq("match_id", row["match_id"]).eq(
                            "predicted_winner", row["predicted_winner"]
                        ).execute()
                    except Exception:
                        pass
                else:
                    logger.warning(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                logger.warning(f"Failed to send Discord alert: {exc}")

            # Respect Discord rate limit (≈ 30 req/min per webhook)
            await asyncio.sleep(2)

    logger.info(f"Discord alerts: {alerts_sent} sent")
    return alerts_sent


async def send_autobet_signals(signals: list[dict]) -> int:
    """
    Post a single Discord embed summarising the value bets placed in this run.

    signals: list of dicts from run_autobet() with keys:
      match, pick, edge, model_prob, market_price, stake
    """
    settings = get_settings()
    webhook_url = getattr(settings, "discord_webhook_url", None)
    if not webhook_url or not signals:
        return 0

    mode = "LIVE 💸" if getattr(settings, "polymarket_live_enabled", False) else "PAPER 📝"

    # Sort by edge descending, cap to top 10 to keep the embed readable
    top = sorted(signals, key=lambda s: s.get("edge", 0), reverse=True)[:10]
    lines = []
    for s in top:
        lines.append(
            f"**{s['pick']}** — {s['match']}\n"
            f"   edge `{s['edge']*100:+.1f}%` · model `{s['model_prob']*100:.0f}%` "
            f"vs mkt `{s['market_price']*100:.0f}%` · stake `${s['stake']:.2f}`"
        )

    embed = {
        "title": f"🎯 Polymarket Value Bets [{mode}]",
        "description": "\n".join(lines),
        "color": 0x9B59B6,
        "footer": {"text": "SportsPick Autobet • not financial advice"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "username": "SportsPick Autobet",
        "avatar_url": "https://em-content.zobj.net/source/apple/391/direct-hit_1f3af.png",
        "embeds": [embed],
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code in (200, 204):
                logger.info(f"Discord autobet signal sent ({len(top)} bets)")
                return 1
            logger.warning(f"Discord autobet webhook returned {resp.status_code}")
    except Exception as exc:
        logger.warning(f"Failed to send autobet signal: {exc}")
    return 0
