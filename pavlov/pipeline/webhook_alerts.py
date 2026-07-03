"""
Post Pavlov signals to a Discord webhook (GitHub Actions / SportsPick unified alerts).

Set DISCORD_WEBHOOK_URL — same webhook as backend/notifications/discord_alerts.py.
Interactive BET/SKIP buttons are not available via webhook; alerts are read-only.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

Venue = Literal["kalshi", "poly", "mlb"]


def _webhook_url() -> str:
    return (
        os.environ.get("DISCORD_WEBHOOK_URL")
        or os.environ.get("PAVLOV_DISCORD_WEBHOOK")
        or ""
    ).strip()


def webhook_enabled() -> bool:
    return bool(_webhook_url())


async def _post(payload: dict) -> bool:
    url = _webhook_url()
    if not url:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            return False
    return True


def _weather_embed(signal: dict, *, venue: Venue) -> dict:
    city = signal.get("city", "Unknown")
    side = str(signal.get("recommended_side", "yes")).upper()
    edge = float(signal.get("edge") or 0)
    venue_label = "Polymarket US" if venue == "poly" else "Kalshi"
    color = 0x00BBFF if venue == "poly" else 0xFFAA00

    return {
        "title": f"🌡️ Pavlov {venue_label} — {city}",
        "color": color,
        "description": signal.get("market_title", "—"),
        "fields": [
            {"name": "Recommendation", "value": f"**BET {side}**", "inline": True},
            {
                "name": "Model / Market",
                "value": (
                    f"{float(signal.get('model_prob', 0)) * 100:.0f}% model\n"
                    f"{float(signal.get('implied_prob', 0)) * 100:.0f}% implied"
                ),
                "inline": True,
            },
            {"name": "Edge", "value": f"**{edge * 100:+.1f}¢**", "inline": True},
            {
                "name": "Kelly",
                "value": (
                    f"${float(signal.get('kelly_dollars', 0)):.2f} "
                    f"({signal.get('kelly_contracts', 0)} ctrs)"
                ),
                "inline": True,
            },
            {
                "name": "Forecast",
                "value": (
                    f"NWS {signal.get('nws_predicted', '?')}°F "
                    f"(thr {signal.get('threshold_f', '?')}°F)"
                ),
                "inline": True,
            },
            {"name": "Strength", "value": str(signal.get("signal_strength", "—")).upper(), "inline": True},
        ],
        "footer": {"text": f"SportsPick · Pavlov weather · {venue_label} · paper review in dashboard"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _mlb_embed(signal: dict) -> dict:
    matchup = signal.get("matchup") or signal.get("market_title") or "MLB game"
    side = str(signal.get("recommended_side", "yes")).upper()
    edge = float(signal.get("edge") or 0)
    pick = signal.get("yes_team") or signal.get("focus_pitcher_name") or side

    return {
        "title": f"⚾ Pavlov MLB — {matchup}",
        "color": 0x3B82F6,
        "fields": [
            {"name": "Pick", "value": f"**{pick}** ({side})", "inline": True},
            {
                "name": "Model / Market",
                "value": (
                    f"{float(signal.get('model_prob', 0)) * 100:.0f}% model\n"
                    f"{float(signal.get('implied_prob', 0)) * 100:.0f}% implied"
                ),
                "inline": True,
            },
            {"name": "Edge", "value": f"**{edge * 100:+.1f}¢**", "inline": True},
            {
                "name": "Confidence",
                "value": f"{float(signal.get('model_confidence', 0)) * 100:.0f}%",
                "inline": True,
            },
            {
                "name": "Kelly",
                "value": f"${float(signal.get('kelly_dollars', 0)):.2f}",
                "inline": True,
            },
            {
                "name": "Pitchers",
                "value": (
                    f"{signal.get('away_pitcher_name', '?')} @ "
                    f"{signal.get('home_pitcher_name', '?')}"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": "SportsPick · Pavlov MLB · Polymarket"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def post_weather_signal(signal: dict, *, venue: Venue = "kalshi") -> bool:
    prefix = "🟦 Poly" if venue == "poly" else "🟩 Kalshi"
    content = f"{prefix} weather signal — use Polymarket/Kalshi app to act (webhook mode)"
    return await _post({
        "content": content,
        "embeds": [_weather_embed(signal, venue=venue)],
    })


async def post_mlb_signal(signal: dict) -> bool:
    return await _post({
        "content": "⚾ Pavlov MLB signal — review in Polymarket (webhook mode)",
        "embeds": [_mlb_embed(signal)],
    })


async def post_text(title: str, body: str, *, color: int = 0x6366F1) -> bool:
    return await _post({
        "embeds": [{
            "title": title,
            "description": body[:4000],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    })
