"""MLB Polymarket Discord: embeds, interactive views, `/mlbpoly*` slash commands."""

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
from polymarket import poly_order_manager

logger = logging.getLogger(__name__)


def _load_pending() -> dict:
    try:
        with open(poly_paths.PENDING_MLB_POLY, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending(data: dict) -> None:
    os.makedirs(os.path.dirname(poly_paths.PENDING_MLB_POLY), exist_ok=True)
    with open(poly_paths.PENDING_MLB_POLY, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def _prune_pending(pending: dict, max_age_hours: int = 48) -> dict:
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


def _load_positions() -> list[dict]:
    try:
        with open(poly_paths.POSITIONS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _mlb_poly_enabled() -> bool:
    """MLB auto-bet flag, falling back to general Polymarket auto-bet."""
    raw = CONFIG.get("POLY_MLB_AUTO_BET_ENABLED")
    if raw is None or str(raw).strip() == "" or int(raw or 0) == 0:
        return truthy_config_int(CONFIG.get("POLY_AUTO_BET_ENABLED"))
    return truthy_config_int(raw)


def _breakdown_text(signal: dict) -> str:
    br = signal.get("probability_breakdown") or {}
    if not br:
        return "No breakdown stored for this signal."
    lines = [
        f"final_home_prob: {br.get('final_home_prob', '—')}",
        f"pitcher_prob: {br.get('pitcher_prob', '—')}",
        f"bullpen_prob: {br.get('bullpen_prob', '—')}",
        f"form_prob: {br.get('form_prob', '—')}",
        f"travel_prob: {br.get('travel_prob', '—')}",
    ]
    return "```\n" + "\n".join(lines)[:1800] + "\n```"


class MLBPolySignalView(discord.ui.View):
    """BET / SKIP / BREAKDOWN for MLB Polymarket alerts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ BET IT",
        style=discord.ButtonStyle.success,
        custom_id="mlb_poly_bet",
    )
    async def bet_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        from pipeline.discord_bot import build_mlb_signal_embed

        await interaction.response.defer()
        msg_id = str(interaction.message.id) if interaction.message else ""
        pending = _load_pending()
        signal = pending.get(msg_id)
        if not signal:
            await interaction.edit_original_response(
                content="❌ This MLB signal has expired or was already actioned.",
                embed=None,
                view=None,
            )
            return
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(
            content="⏳ Placing MLB Polymarket order…", embed=None, view=self
        )
        signal.setdefault("placed_via", "manual")
        result = await poly_order_manager.place_mlb_trade(signal)
        if result["success"]:
            embed = build_mlb_signal_embed(signal, status="placed")
            await interaction.edit_original_response(content="", embed=embed, view=self)
        else:
            await interaction.edit_original_response(
                content=f"❌ Order failed: {result.get('error', 'unknown error')}",
                embed=None,
                view=None,
            )
        pending.pop(msg_id, None)
        _save_pending(pending)

    @discord.ui.button(
        label="❌ SKIP",
        style=discord.ButtonStyle.danger,
        custom_id="mlb_poly_skip",
    )
    async def skip_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        from pipeline.discord_bot import build_mlb_signal_embed

        msg_id = str(interaction.message.id) if interaction.message else ""
        pending = _load_pending()
        signal = pending.get(msg_id)
        if not signal:
            await interaction.response.send_message(
                "This signal has expired.",
                ephemeral=True,
            )
            return
        for item in self.children:
            item.disabled = True
        poly_order_manager.log_mlb_skip(signal)
        embed = build_mlb_signal_embed(signal, status="skipped")
        await interaction.response.edit_message(embed=embed, view=self)
        pending.pop(msg_id, None)
        _save_pending(pending)

    @discord.ui.button(
        label="🔍 BREAKDOWN",
        style=discord.ButtonStyle.secondary,
        custom_id="mlb_poly_breakdown",
    )
    async def breakdown_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        msg_id = str(interaction.message.id) if interaction.message else ""
        pending = _load_pending()
        signal = pending.get(msg_id) or {}
        txt = _breakdown_text(signal)
        await interaction.response.send_message(txt, ephemeral=True)


async def send_mlb_poly_signal_to_channel(
    channel: discord.abc.Messageable,
    signal: dict,
) -> None:
    from pipeline.discord_bot import build_mlb_signal_embed

    embed = build_mlb_signal_embed(signal, status="pending")
    view = MLBPolySignalView()
    msg = await channel.send(embed=embed, view=view)
    sig = dict(signal)
    sig["posted_at"] = datetime.now(timezone.utc).isoformat()
    pending = _prune_pending(_load_pending(), max_age_hours=48)
    pending[str(msg.id)] = sig
    _save_pending(pending)
    logger.info(
        "mlb_poly_discord: signal %s game=%s msg=%s",
        signal.get("ticker"),
        signal.get("game_id"),
        msg.id,
    )


async def send_mlb_poly_signal(signal: dict) -> None:
    from pipeline.discord_bot import get_mlb_poly_channel

    channel = get_mlb_poly_channel()
    if channel is None:
        logger.warning("mlb_poly_discord: no MLB Poly channel — skip send.")
        return
    await send_mlb_poly_signal_to_channel(channel, signal)


async def send_mlb_daily_summary_embed() -> None:
    from pipeline import learning_loop
    from pipeline.discord_bot import get_channel, get_mlb_poly_channel

    ch = get_mlb_poly_channel() or get_channel()
    if ch is None:
        logger.warning("mlb_poly_discord: daily summary skipped — no channel")
        return
    stats = learning_loop.generate_mlb_daily_summary(24)
    embed = discord.Embed(title="⚾ MLB Polymarket — Daily Summary", color=0x378ADD)
    embed.add_field(name="P&L (24h)", value=f"${stats.get('pl', 0):+.2f}", inline=True)
    embed.add_field(
        name="Record",
        value=f"{stats.get('wins', 0)}W / {stats.get('losses', 0)}L "
        f"({stats.get('win_rate', 0) * 100:.0f}%)",
        inline=True,
    )
    embed.add_field(name="Bankroll", value=f"${stats.get('bankroll', 0):.2f}", inline=True)
    embed.add_field(name="Signals (24h)", value=str(stats.get("signals_fired", 0)), inline=True)
    embed.add_field(name="Best pitcher", value=str(stats.get("best_pitcher", "—")), inline=True)
    embed.add_field(name="Worst pitcher", value=str(stats.get("worst_pitcher", "—")), inline=True)
    embed.add_field(
        name="Top park × edge",
        value=str(stats.get("top_park_factor_edge", "—"))[:1024],
        inline=False,
    )
    embed.set_footer(text="Pavlov MLB Bot · 7am ET")
    await ch.send(embed=embed)


async def send_mlb_poly_auto_bet_alert(
    signal: dict,
    order_id: str | None,
    error: str | None = None,
) -> None:
    from pipeline.discord_bot import get_mlb_poly_channel

    channel = get_mlb_poly_channel()
    if channel is None:
        return
    matchup = f"{signal.get('away_team_abbr')} @ {signal.get('home_team_abbr')}"
    if error:
        embed = discord.Embed(
            title=f"⚠️ MLB POLY AUTO-BET FAILED — {matchup}",
            description=f"```{error}```",
            color=0xFF4444,
        )
    else:
        embed = discord.Embed(
            title=f"🤖 MLB POLY AUTO-BET — {matchup}",
            color=0x00BBFF,
        )
    embed.add_field(name="Market", value=signal.get("market_title") or signal.get("title") or "—", inline=False)
    side = str(signal.get("recommended_side", "yes")).upper()
    embed.add_field(name="Side", value=side, inline=True)
    embed.add_field(
        name="Size",
        value=f"${signal.get('kelly_dollars', 0):.2f} ({signal.get('kelly_contracts')} @ {signal.get('yes_price')}¢)",
        inline=True,
    )
    if order_id:
        embed.set_footer(text=f"Order: {order_id} | MLB Polymarket")
    await channel.send(embed=embed)


async def post_mlb_poly_resolution(position: dict) -> None:
    from pipeline.discord_bot import get_mlb_poly_channel

    channel = get_mlb_poly_channel()
    if channel is None:
        return
    won = position.get("status") == "won"
    pl = float(position.get("pl") or 0.0)
    matchup = f"{position.get('away_team_abbr')} @ {position.get('home_team_abbr')}"
    icon = "🟢" if won else "🔴"
    embed = discord.Embed(
        title=f"[MLB POLY] {icon} {'WIN' if won else 'LOSS'} — {matchup}",
        description=f"P&L: **${pl:+.2f}**",
        color=0x00FF88 if won else 0xFF4444,
    )
    embed.add_field(name="Ticker", value=str(position.get("ticker")), inline=True)
    embed.add_field(name="Game", value=str(position.get("game_id")), inline=True)
    embed.set_footer(text=f"{position.get('resolved_at', '')} | Pavlov MLB Bot")
    await channel.send(embed=embed)


def register_mlb_poly_app_commands(tree: app_commands.CommandTree) -> None:
    @tree.command(name="mlbpolystatus", description="MLB Polymarket: bankroll, open MLB positions, 24h summary")
    async def slash_mlbpolystatus(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from pipeline import learning_loop

        try:
            loop = asyncio.get_event_loop()
            bal = 0.0
            if poly_client.poly_configured():
                bal = await loop.run_in_executor(None, poly_client.get_account_balance)
            stats = learning_loop.generate_mlb_daily_summary(hours=24)
            open_mlb = [
                p
                for p in _load_positions()
                if p.get("venue") == "mlb_poly" and p.get("status") == "open"
            ]
            embed = discord.Embed(title="MLB Polymarket — Status", color=0x00CC66)
            embed.add_field(name="Buying power", value=f"${bal:.2f}", inline=True)
            embed.add_field(name="Open MLB", value=str(len(open_mlb)), inline=True)
            embed.add_field(name="24h P&L", value=f"${stats['pl']:+.2f}", inline=True)
            embed.add_field(
                name="24h W/L",
                value=f"{stats['wins']}/{stats['losses']} ({stats['win_rate'] * 100:.0f}%)",
                inline=True,
            )
            embed.add_field(name="Signals (24h)", value=str(stats["signals_fired"]), inline=True)
            embed.add_field(name="Best pitcher", value=str(stats.get("best_pitcher", "—")), inline=True)
            embed.add_field(name="Worst pitcher", value=str(stats.get("worst_pitcher", "—")), inline=True)
            embed.add_field(
                name="Top park×edge",
                value=str(stats.get("top_park_factor_edge", "—"))[:1024],
                inline=False,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("mlb_poly_discord: /mlbpolystatus — %s", exc)
            await interaction.followup.send(f"Error: `{exc}`", ephemeral=True)

    @tree.command(name="mlbpolypositions", description="Open MLB Polymarket positions (bot log)")
    async def slash_mlbpolypositions(interaction: discord.Interaction) -> None:
        open_mlb = [
            p
            for p in _load_positions()
            if p.get("venue") == "mlb_poly" and p.get("status") == "open"
        ]
        if not open_mlb:
            await interaction.response.send_message("No open MLB Polymarket positions.", ephemeral=True)
            return
        embed = discord.Embed(title="MLB Polymarket — Open", color=0xFFAA00)
        for p in open_mlb[:10]:
            m = f"{p.get('away_team_abbr')} @ {p.get('home_team_abbr')}"
            embed.add_field(
                name=m,
                value=(
                    f"{str(p.get('recommended_side', '')).upper()} "
                    f"{p.get('kelly_contracts', 1)}× @ {p.get('price_cents')}¢ — {p.get('ticker')}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="mlbpolypnl", description="MLB Polymarket resolved P&L (24h window)")
    async def slash_mlbpolypnl(interaction: discord.Interaction) -> None:
        from pipeline import learning_loop

        stats = learning_loop.generate_mlb_daily_summary(hours=24)
        await interaction.response.send_message(
            f"24h MLB Poly P&L: **${stats['pl']:+.2f}** ({stats['wins']}W / {stats['losses']}L)",
            ephemeral=True,
        )

    @tree.command(name="mlbpolyautobet", description="Toggle MLB Polymarket auto-bet (or status)")
    @app_commands.describe(state="on, off, or status")
    async def slash_mlbpolyautobet(interaction: discord.Interaction, state: str = "status") -> None:
        state = state.lower().strip()
        if state == "status":
            en = _mlb_poly_enabled()
            raw_mlb = CONFIG.get("POLY_MLB_AUTO_BET_ENABLED")
            raw_poly = CONFIG.get("POLY_AUTO_BET_ENABLED")
            await interaction.response.send_message(
                f"MLB Polymarket auto-bet: **{'ON' if en else 'OFF'}** "
                f"(POLY_MLB_AUTO_BET_ENABLED={raw_mlb!r}, fallback POLY_AUTO_BET_ENABLED={raw_poly!r}). "
                f"Set `POLY_MLB_AUTO_BET_ENABLED=1` on Railway to persist.",
                ephemeral=True,
            )
            return
        if state not in ("on", "off"):
            await interaction.response.send_message("Usage: `/mlbpolyautobet on|off|status`", ephemeral=True)
            return
        CONFIG["POLY_MLB_AUTO_BET_ENABLED"] = 1 if state == "on" else 0
        logger.warning("mlb_poly_discord: POLY_MLB_AUTO_BET_ENABLED toggled to %s", state)
        await interaction.response.send_message(
            f"MLB Polymarket auto-bet: **{state.upper()}** (resets on deploy unless env set).",
            ephemeral=True,
        )


async def maybe_auto_bet_mlb(signals: list[dict]) -> None:
    """If enabled, place the top MLB signal when Poly is configured."""
    if not signals or not _mlb_poly_enabled():
        return
    if not poly_client.poly_configured():
        return

    try:
        with open(poly_paths.POSITIONS, "r", encoding="utf-8") as fh:
            positions = json.load(fh)
        if not isinstance(positions, list):
            positions = []
    except (FileNotFoundError, json.JSONDecodeError):
        positions = []

    open_games = {
        int(p["game_id"])
        for p in positions
        if p.get("venue") == "mlb_poly"
        and p.get("status") == "open"
        and p.get("game_id") is not None
    }

    top = None
    for sig in signals:
        gid = sig.get("game_id")
        if gid is None:
            continue
        if int(gid) in open_games:
            continue
        top = sig
        break
    if top is None:
        return

    if float(top.get("kelly_dollars") or 0) <= 0:
        return
    top = dict(top)
    top["placed_via"] = "auto_mlb"
    try:
        result = await poly_order_manager.place_mlb_trade(top)
        if result.get("success"):
            await send_mlb_poly_auto_bet_alert(top, result.get("order_id"), None)
        else:
            await send_mlb_poly_auto_bet_alert(top, None, result.get("error", "unknown"))
    except Exception as exc:
        logger.exception("maybe_auto_bet_mlb: %s", exc)
        await send_mlb_poly_auto_bet_alert(top, None, str(exc))
