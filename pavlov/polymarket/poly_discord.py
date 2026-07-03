"""Polymarket US Discord: interactive views, alerts, and /poly* slash commands.

Registers on the shared ``CommandTree`` from ``pipeline.discord_bot`` so one bot
serves both Kalshi and Polymarket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
import discord.ui
from discord import app_commands

from config import CONFIG, truthy_config_int

from polymarket import paths as poly_paths
from polymarket import poly_client
from polymarket import poly_learning_loop
from polymarket import poly_order_manager

logger = logging.getLogger(__name__)


def _load_poly_pending() -> dict:
    try:
        with open(poly_paths.PENDING_SIGNALS, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_poly_pending(data: dict) -> None:
    os.makedirs(os.path.dirname(poly_paths.PENDING_SIGNALS), exist_ok=True)
    with open(poly_paths.PENDING_SIGNALS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def _prune_poly_pending(pending: dict, max_age_hours: int = 48) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    out: dict = {}
    for msg_id, sig in pending.items():
        ts = sig.get("posted_at", "")
        try:
            posted = datetime.fromisoformat(ts)
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if posted >= cutoff:
                out[msg_id] = sig
        except (ValueError, TypeError):
            continue
    return out


def _load_poly_positions() -> list[dict]:
    try:
        with open(poly_paths.POSITIONS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


class PolySignalView(discord.ui.View):
    """Persistent BET/SKIP for Polymarket US signals."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ BET",
        style=discord.ButtonStyle.success,
        custom_id="poly_bet",
    )
    async def bet_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        from pipeline.discord_bot import build_signal_embed

        await interaction.response.defer()
        msg_id = str(interaction.message.id) if interaction.message else ""
        pending = _load_poly_pending()
        signal = pending.get(msg_id)
        if not signal:
            await interaction.edit_original_response(
                content="❌ This Polymarket signal has expired or was already actioned.",
                embed=None,
                view=None,
            )
            return
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(
            content="⏳ Placing Polymarket order…", embed=None, view=self
        )

        signal.setdefault("placed_via", "manual")
        result = await poly_order_manager.place_trade(signal)
        if result["success"]:
            embed = build_signal_embed(
                signal,
                status="placed",
                order_id=result.get("order_id"),
                venue="poly",
            )
            await interaction.edit_original_response(content="", embed=embed, view=self)
        else:
            await interaction.edit_original_response(
                content=f"❌ Order failed: {result.get('error', 'unknown error')}",
                embed=None,
                view=None,
            )
        pending.pop(msg_id, None)
        _save_poly_pending(pending)

    @discord.ui.button(
        label="❌ SKIP",
        style=discord.ButtonStyle.danger,
        custom_id="poly_skip",
    )
    async def skip_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        from pipeline.discord_bot import build_signal_embed

        msg_id = str(interaction.message.id) if interaction.message else ""
        pending = _load_poly_pending()
        signal = pending.get(msg_id)
        if not signal:
            await interaction.response.send_message(
                "This signal has expired.",
                ephemeral=True,
            )
            return
        for item in self.children:
            item.disabled = True
        poly_order_manager.log_skip(signal)
        embed = build_signal_embed(signal, status="skipped", venue="poly")
        await interaction.response.edit_message(embed=embed, view=self)
        pending.pop(msg_id, None)
        _save_poly_pending(pending)


async def send_poly_signal(signal: dict) -> None:
    """Post a Polymarket US signal to the poly channel (or main)."""
    from pipeline import webhook_alerts

    if webhook_alerts.webhook_enabled():
        await webhook_alerts.post_weather_signal(signal, venue="poly")
        logger.info(
            "poly_discord: POLY signal via webhook for %s.", signal.get("ticker")
        )
        return

    from pipeline.discord_bot import build_signal_embed, get_channel, get_poly_channel

    channel = get_poly_channel() or get_channel()
    if channel is None:
        logger.warning("poly_discord: send_poly_signal skipped — Discord channel not ready.")
        return

    embed = build_signal_embed(signal, status="pending", venue="poly")
    view = PolySignalView()
    msg = await channel.send(embed=embed, view=view)
    sig = dict(signal)
    sig["posted_at"] = datetime.now(timezone.utc).isoformat()
    pending = _prune_poly_pending(_load_poly_pending(), max_age_hours=48)
    pending[str(msg.id)] = sig
    _save_poly_pending(pending)
    logger.info(
        "poly_discord: POLY signal for %s (edge=%.4f, msg_id=%s).",
        signal.get("ticker"),
        signal.get("edge", 0),
        msg.id,
    )


async def send_poly_auto_bet_alert(
    signal: dict,
    order_id: str | None,
    error: str | None = None,
) -> None:
    from pipeline.discord_bot import get_channel, get_poly_channel

    channel = get_poly_channel() or get_channel()
    if channel is None:
        logger.warning(
            "poly_discord: send_poly_auto_bet_alert skipped — Discord channel not ready."
        )
        return

    city = signal.get("city", "Unknown")
    side = signal.get("recommended_side", "yes").upper()

    if error:
        embed = discord.Embed(
            title=f"⚠️ POLY AUTO-BET FAILED  —  {city}",
            description=f"```{error}```",
            color=0xFF4444,
        )
    else:
        embed = discord.Embed(
            title=f"🤖 POLY AUTO-BET PLACED  —  {city}",
            color=0x00BBFF,
        )

    embed.add_field(
        name="Market",
        value=signal.get("market_title", "—"),
        inline=False,
    )
    embed.add_field(
        name="Side",
        value=f"{'✅' if side == 'YES' else '❌'} {side}",
        inline=True,
    )
    embed.add_field(
        name="Cost",
        value=f"${signal.get('kelly_dollars', 0):.2f}  "
        f"({signal.get('kelly_contracts', 0)} @ "
        f"{int(signal.get('implied_prob', 0) * 100)}¢)",
        inline=True,
    )
    embed.add_field(
        name="Edge",
        value=f"{signal.get('edge', 0) * 100:+.1f}¢",
        inline=True,
    )
    embed.add_field(
        name="Model",
        value=f"{signal.get('model_prob', 0) * 100:.0f}%  "
        f"(ens {signal.get('ensemble_mean', '—')}°F "
        f"±{signal.get('ensemble_spread', '—')}°F)",
        inline=False,
    )
    if order_id:
        embed.set_footer(text=f"Order: {order_id} | Polymarket US")
    else:
        embed.set_footer(text="Polymarket US")

    await channel.send(embed=embed)
    logger.info(
        "poly_discord: poly auto-bet alert for %s (success=%s).",
        signal.get("ticker"),
        error is None,
    )


async def post_poly_resolution_update(position: dict) -> None:
    from pipeline.discord_bot import get_channel, get_poly_channel

    channel = get_poly_channel() or get_channel()
    if channel is None:
        logger.warning(
            "poly_discord: post_poly_resolution_update skipped — Discord channel not ready."
        )
        return
    won = position.get("status") == "won"
    pl = position.get("pl", 0.0)
    city = position.get("city", position.get("ticker", "?"))
    side = position.get("recommended_side", "?").upper()
    color = 0x00FF88 if won else 0xFF4444
    icon = "🟢" if won else "🔴"
    embed = discord.Embed(
        title=f"[POLY US] {icon} {'WIN' if won else 'LOSS'} — {city}",
        description=(
            f"**{side}** bet settled {'in your favour' if won else 'against you'}.\n"
            f"P&L: **${pl:+.2f}**"
        ),
        color=color,
    )
    embed.add_field(name="Slug", value=position.get("ticker", "?"), inline=True)
    embed.add_field(name="Contracts", value=str(position.get("kelly_contracts", 1)), inline=True)
    embed.add_field(name="Entry", value=f"{position.get('price_cents', '?')}¢", inline=True)
    embed.set_footer(
        text=f"Resolved at {position.get('resolved_at', '')} | Pavlov · Polymarket US"
    )
    await channel.send(embed=embed)
    logger.info(
        "poly_discord: POLY resolution for %s (%s $%.2f)",
        position.get("ticker"),
        "WIN" if won else "LOSS",
        pl,
    )


def register_poly_app_commands(tree: app_commands.CommandTree) -> None:
    """Attach /poly* slash commands to the shared command tree."""

    @tree.command(name="polystatus", description="Polymarket US: bankroll, open positions, 24h P&L")
    async def slash_polystatus(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not poly_client.poly_configured():
            await interaction.followup.send(
                "Polymarket US is not configured — set `POLY_KEY_ID` and `POLY_SECRET_KEY`.",
                ephemeral=True,
            )
            return
        try:
            loop = asyncio.get_event_loop()
            balance = await loop.run_in_executor(None, poly_client.get_account_balance)
            stats = poly_learning_loop.generate_summary(hours=24)
            positions = [p for p in _load_poly_positions() if p.get("status") == "open"]
            embed = discord.Embed(title="Polymarket US — Status", color=0x9B59B6)
            embed.add_field(name="Buying power", value=f"${balance:.2f}", inline=True)
            embed.add_field(name="Open (Poly)", value=str(len(positions)), inline=True)
            embed.add_field(name="24h P&L", value=f"${stats['pl']:+.2f}", inline=True)
            embed.add_field(name="24h wins", value=f"{stats['wins']}/{stats['total']}", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("poly_discord: /polystatus — %s", exc)
            await interaction.followup.send(f"Error: `{exc}`", ephemeral=True)

    @tree.command(name="polypositions", description="Polymarket US open positions")
    async def slash_polypositions(interaction: discord.Interaction) -> None:
        positions = [p for p in _load_poly_positions() if p.get("status") == "open"]
        if not positions:
            await interaction.response.send_message("No open Polymarket positions.", ephemeral=True)
            return
        embed = discord.Embed(title="Polymarket US — Open", color=0xFFAA00)
        for p in positions[:8]:
            embed.add_field(
                name=p.get("city", p.get("ticker")),
                value=(
                    f"{p.get('recommended_side', '?').upper()} "
                    f"{p.get('kelly_contracts', 1)}× @ {p.get('price_cents', '?')}¢ | "
                    f"{p.get('ticker')}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="polypnl", description="Polymarket US P&L breakdown by city (resolved)")
    async def slash_polypnl(interaction: discord.Interaction) -> None:
        positions = [p for p in _load_poly_positions() if p.get("status") in ("won", "lost")]
        by_city: dict[str, float] = {}
        for p in positions:
            city = p.get("city", "Unknown")
            by_city[city] = round(by_city.get(city, 0.0) + p.get("pl", 0.0), 4)
        if not by_city:
            await interaction.response.send_message(
                "No resolved Polymarket trades yet.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(title="Polymarket US — P&L by city", color=0x00FF88)
        for city, pl in sorted(by_city.items(), key=lambda x: -abs(x[1])):
            icon = "✅" if pl >= 0 else "❌"
            embed.add_field(name=city, value=f"{icon} ${pl:+.2f}", inline=True)
        total_pl = sum(by_city.values())
        embed.set_footer(text=f"Total: ${total_pl:+.2f} across {len(positions)} trades")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="polyautobet", description="Toggle Polymarket auto-bet (or status)")
    @app_commands.describe(state="on, off, or status")
    async def slash_polyautobet(interaction: discord.Interaction, state: str = "status") -> None:
        try:
            state = state.lower().strip()
            if state == "status":
                en = truthy_config_int(CONFIG.get("POLY_AUTO_BET_ENABLED"))
                await interaction.response.send_message(
                    f"Polymarket auto-bet: **{'ON' if en else 'OFF'}** "
                    f"(CONFIG POLY_AUTO_BET_ENABLED={CONFIG.get('POLY_AUTO_BET_ENABLED')!r}; "
                    f"`/autobet` is **Kalshi only**.) "
                    f"Persist on Railway with env `POLY_AUTO_BET_ENABLED=1`.",
                    ephemeral=True,
                )
                return
            if state not in ("on", "off"):
                await interaction.response.send_message(
                    "Usage: `/polyautobet on|off|status`",
                    ephemeral=True,
                )
                return
            CONFIG["POLY_AUTO_BET_ENABLED"] = 1 if state == "on" else 0
            logger.warning("poly_discord: POLY_AUTO_BET_ENABLED toggled %s by user.", state.upper())
            await interaction.response.send_message(
                f"Polymarket auto-bet: **{state.upper()}** (resets on deploy unless env set).",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("/polyautobet — %s", exc)
            try:
                await interaction.response.send_message(f"Error: `{exc}`", ephemeral=True)
            except Exception:
                pass
