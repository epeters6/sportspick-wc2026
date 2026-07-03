"""MLB Polymarket Discord: embeds, interactive views, `/mlbpoly*` slash commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
import discord.ui
from discord import app_commands

from config import CONFIG, truthy_config_int
from pipeline import mlb_ingame_learning
from pipeline.mlb_ingame_bets import (
    count_pregame_autos_today_et,
    game_has_pregame_auto_today,
    has_open_pregame_auto,
)
from polymarket import mlb_autobet_state
from polymarket import poly_client
from polymarket import poly_order_manager

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _load_pending() -> dict:
    path = mlb_autobet_state.mlb_pending_mlb_poly_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending(data: dict) -> None:
    path = mlb_autobet_state.mlb_pending_mlb_poly_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def _game_has_open_mlb_poly_position(game_id: int) -> bool:
    """True if there is an open ``mlb_poly`` position for this game (manual or auto)."""
    for pos in _load_positions():
        if pos.get("venue") != "mlb_poly":
            continue
        if pos.get("status") != "open":
            continue
        try:
            if int(pos.get("game_id") or 0) == int(game_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


def expire_stale_pending_mlb_signals(hours: float | None = None) -> int:
    """Treat pending Discord alerts with no action after *hours* as manual skips.

    Writes the same learning row as clicking SKIP (so pitcher/team weights update
    after the game). Drops the pending entry so BET IT shows as expired.

    Returns the number of signals logged as skip. Set ``MLB_PENDING_AUTO_SKIP_HOURS``
  to ``0`` to disable.
    """
    if hours is None:
        hours = float(CONFIG.get("MLB_PENDING_AUTO_SKIP_HOURS") or 1.0)
    if hours <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    pending = _prune_pending(_load_pending(), max_age_hours=48)
    removed = 0
    logged = 0

    for msg_id, sig in list(pending.items()):
        ts = sig.get("posted_at", "")
        try:
            posted = datetime.fromisoformat(str(ts))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if posted > cutoff:
            continue

        gid = sig.get("game_id")
        if gid is not None and _game_has_open_mlb_poly_position(int(gid)):
            pending.pop(msg_id, None)
            removed += 1
            logger.debug(
                "mlb_poly_discord: drop stale pending — open position game_id=%s msg=%s",
                gid,
                msg_id,
            )
            continue

        if poly_order_manager.log_mlb_skip(
            sig,
            learn_reason="pending_no_action_1h",
            learn_source="auto",
        ):
            logged += 1
        else:
            logger.debug(
                "mlb_poly_discord: pending expire — skip already logged %s",
                sig.get("ticker"),
            )
        pending.pop(msg_id, None)
        removed += 1

    if removed:
        _save_pending(pending)
    if logged:
        logger.info(
            "mlb_poly_discord: auto-skipped %d pending signal(s) older than %.1fh",
            logged,
            hours,
        )
    return logged


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
        with open(mlb_autobet_state.mlb_positions_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _mlb_poly_enabled() -> bool:
    """MLB auto-bet flag, falling back to general Polymarket auto-bet."""
    raw = CONFIG.get("POLY_MLB_AUTO_BET_ENABLED")
    if raw is None or str(raw).strip() == "" or int(raw or 0) == 0:
        return truthy_config_int(CONFIG.get("POLY_AUTO_BET_ENABLED"))
    return truthy_config_int(raw)


def mlb_poly_autobet_armed() -> bool:
    """Public alias for scheduler / in-game poll."""
    return _mlb_poly_enabled()


def mlb_poly_pregame_autobet_enabled() -> bool:
    """Master MLB arm on and pregame switch on."""
    return _mlb_poly_enabled() and truthy_config_int(
        CONFIG.get("POLY_MLB_PREGAME_AUTO_ENABLED")
    )


def mlb_poly_ingame_autobet_enabled() -> bool:
    """Master MLB arm on and in-game switch on."""
    return _mlb_poly_enabled() and truthy_config_int(
        CONFIG.get("POLY_MLB_INGAME_ENABLED")
    )


def _mlb_auto_constraints_embed_lines() -> list[str]:
    """Human-readable limits for `/mlbpolyautobet status` (master + both modes)."""
    return [
        "**Master MLB Poly auto** (gates any MLB auto order):",
        f"**{'ON' if _mlb_poly_enabled() else 'OFF'}** — "
        "`POLY_MLB_AUTO_BET_ENABLED` / `POLY_AUTO_BET_ENABLED`",
        "",
        *_pregame_autobet_status_lines(),
        "",
        *_ingame_autobet_status_lines(),
    ]


def _pregame_autobet_status_lines() -> list[str]:
    """Pregame-only limits + switch (for `/mlbpolyautopregame`)."""
    sw = truthy_config_int(CONFIG.get("POLY_MLB_PREGAME_AUTO_ENABLED"))
    eff = mlb_poly_pregame_autobet_enabled()
    min_c = float(CONFIG.get("POLY_MLB_AUTO_MIN_MODEL_CONFIDENCE") or 0.65)
    opt_edge = float(CONFIG.get("POLY_MLB_AUTO_MIN_EDGE") or 0.08)
    max_usd = float(CONFIG.get("POLY_MLB_AUTO_MAX_KELLY_DOLLARS") or 1.5)
    max_c = int(CONFIG.get("POLY_MLB_AUTO_MAX_CONTRACTS") or 1)
    max_day = int(CONFIG.get("POLY_MLB_AUTO_MAX_BETS_PER_ET_DAY") or 3)
    strong = truthy_config_int(CONFIG.get("POLY_MLB_AUTO_REQUIRE_STRONG"))
    max_implied = float(CONFIG.get("POLY_MLB_AUTO_MAX_IMPLIED") or 0.85)
    min_implied = float(CONFIG.get("POLY_MLB_AUTO_MIN_IMPLIED") or 0.15)
    out = [
        f"**Pregame auto** — switch `POLY_MLB_PREGAME_AUTO_ENABLED` = **{'ON' if sw else 'OFF'}**",
        f"**Effective** (master ∧ switch): **{'ON' if eff else 'OFF'}**",
        f"Min confidence: **{min_c:.2f}** · min |edge|: **{opt_edge:.2f}** · cap **${max_usd:.2f}** / **{max_c}** contracts",
        f"Implied band: **{min_implied:.2f}** ≤ imp ≤ **{max_implied:.2f}** (block extreme tails)",
        f"Max **{max_day}** pregame auto-bets / ET day · 1 per game · strong-only: **{'yes' if strong else 'no'}**",
    ]
    return out


def _ingame_autobet_status_lines() -> list[str]:
    """In-game limits + switch (for `/mlbpolyautoingame`)."""
    sw = truthy_config_int(CONFIG.get("POLY_MLB_INGAME_ENABLED"))
    eff = mlb_poly_ingame_autobet_enabled()
    poll = int(CONFIG.get("POLY_MLB_INGAME_POLL_MINUTES") or 12)
    imax_usd = float(CONFIG.get("POLY_MLB_INGAME_MAX_KELLY_DOLLARS") or 2.0)
    imax_c = int(CONFIG.get("POLY_MLB_INGAME_MAX_CONTRACTS") or 1)
    imax_day = int(CONFIG.get("POLY_MLB_INGAME_MAX_BETS_PER_ET_DAY") or 5)
    return [
        f"**In-game auto** — `POLY_MLB_INGAME_ENABLED` = **{'ON' if sw else 'OFF'}**",
        f"**Effective** (master ∧ switch): **{'ON' if eff else 'OFF'}**",
        f"Poll **{poll}m** · cap **${imax_usd:.2f}** / **{imax_c}** contracts",
        f"≤**{imax_day}** / ET day · 1 per game · learned: `{mlb_ingame_learning.summary_line()}`",
    ]


def _breakdown_text(signal: dict) -> str:
    br = signal.get("probability_breakdown") or {}
    if not br:
        return "No breakdown stored for this signal."

    def _fmt_edge(key: str) -> str:
        v = br.get(key)
        if v is None:
            return "—"
        try:
            return f"{float(v) * 100:+.1f}%"
        except (TypeError, ValueError):
            return str(v)

    def _fmt_prob(key: str) -> str:
        v = br.get(key)
        if v is None:
            return "—"
        try:
            return f"{float(v) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(v)

    lines = [
        f"final_home_prob: {_fmt_prob('final_home_prob')}",
        f"raw_edge:        {_fmt_edge('raw_edge')}",
        f"home_field:      {_fmt_edge('home_field_edge')}",
        f"pitcher_edge:    {_fmt_edge('pitcher_edge')}",
        f"bullpen_edge:    {_fmt_edge('bullpen_edge')}",
        f"form_edge:       {_fmt_edge('form_edge')}",
        f"lineup_edge:     {_fmt_edge('lineup_edge')}",
        f"travel_edge:     {_fmt_edge('travel_edge')}",
        f"coors_penalty:   {_fmt_edge('coors_penalty')}",
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
    import sys
    from pathlib import Path

    _root = Path(__file__).resolve().parents[2]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    try:
        from pipeline import webhook_alerts

        if webhook_alerts.webhook_enabled():
            await webhook_alerts.post_mlb_signal(signal)
            logger.info(
                "mlb_poly_discord: MLB signal via webhook for %s",
                signal.get("ticker"),
            )
            return
    except ImportError:
        pass

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
    if signal.get("ingame_context"):
        ctx = signal["ingame_context"]
        embed.add_field(
            name="In-game snapshot",
            value=(
                f"Score **{ctx.get('away_runs', '?')}–{ctx.get('home_runs', '?')}** "
                f"(R{ctx.get('run_diff')}) · inn **{ctx.get('inning')}** "
                f"· side imp **{ctx.get('implied_side')}**"
            ),
            inline=False,
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
        await interaction.response.defer(ephemeral=True)
        open_mlb = [
            p
            for p in _load_positions()
            if p.get("venue") == "mlb_poly" and p.get("status") == "open"
        ]
        if not open_mlb:
            await interaction.followup.send("No open MLB Polymarket positions.", ephemeral=True)
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
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="mlbpolypnl", description="MLB Polymarket resolved P&L (24h window)")
    async def slash_mlbpolypnl(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from pipeline import learning_loop

        stats = learning_loop.generate_mlb_daily_summary(hours=24)
        await interaction.followup.send(
            f"24h MLB Poly P&L: **${stats['pl']:+.2f}** ({stats['wins']}W / {stats['losses']}L)",
            ephemeral=True,
        )

    @tree.command(name="mlbpolyautobet", description="Toggle MLB Polymarket auto-bet (or status)")
    @app_commands.describe(state="on, off, or status")
    async def slash_mlbpolyautobet(interaction: discord.Interaction, state: str = "status") -> None:
        await interaction.response.defer(ephemeral=True)
        state = state.lower().strip()
        if state == "status":
            en = _mlb_poly_enabled()
            raw_mlb = CONFIG.get("POLY_MLB_AUTO_BET_ENABLED")
            raw_poly = CONFIG.get("POLY_AUTO_BET_ENABLED")
            lines = [
                f"**Master (any MLB auto):** {'ON' if en else 'OFF'}",
                f"`POLY_MLB_AUTO_BET_ENABLED`={raw_mlb!r} (fallback `POLY_AUTO_BET_ENABLED`={raw_poly!r})",
                "",
                "**Per-mode:** `/mlbpolyautopregame` (pregame) · `/mlbpolyautoingame` (live)**",
                "",
                *_mlb_auto_constraints_embed_lines(),
                "",
                "Set env on Railway to persist across deploys.",
            ]
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return
        if state not in ("on", "off"):
            await interaction.followup.send("Usage: `/mlbpolyautobet on|off|status`", ephemeral=True)
            return
        CONFIG["POLY_MLB_AUTO_BET_ENABLED"] = 1 if state == "on" else 0
        if state == "on":
            mlb_autobet_state.arm_autobet_suppress()
        logger.warning("mlb_poly_discord: POLY_MLB_AUTO_BET_ENABLED toggled to %s", state)
        await interaction.followup.send(
            f"MLB Polymarket **master** auto arm: **{state.upper()}**. "
            "Pregame/in-game toggles: `/mlbpolyautopregame` · `/mlbpolyautoingame`. "
            "Resets on deploy unless env set.",
            ephemeral=True,
        )


    @tree.command(
        name="mlbpolyautopregame",
        description="Pregame MLB Poly auto-bet: on, off, or status",
    )
    @app_commands.describe(state="on, off, or status")
    async def slash_mlbpolyautopregame(
        interaction: discord.Interaction, state: str = "status"
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        state = state.lower().strip()
        if state == "status":
            lines = [
                "**`/mlbpolyautopregame` — pregame auto only**",
                "",
                *_pregame_autobet_status_lines(),
                "",
                "Master must be ON (`/mlbpolyautobet`). "
                "Persist: `POLY_MLB_PREGAME_AUTO_ENABLED` on Railway.",
            ]
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return
        if state not in ("on", "off"):
            await interaction.followup.send(
                "Usage: `/mlbpolyautopregame on|off|status`", ephemeral=True
            )
            return
        CONFIG["POLY_MLB_PREGAME_AUTO_ENABLED"] = 1 if state == "on" else 0
        if state == "on":
            mlb_autobet_state.arm_autobet_suppress()
        logger.warning(
            "mlb_poly_discord: POLY_MLB_PREGAME_AUTO_ENABLED toggled to %s", state
        )
        eff = mlb_poly_pregame_autobet_enabled()
        await interaction.followup.send(
            f"Pregame MLB Poly auto-bet: **{state.upper()}** · "
            f"effective (master ∧ switch): **{'ON' if eff else 'OFF'}**. "
            "Resets on deploy unless env set.",
            ephemeral=True,
        )

    @tree.command(
        name="mlbpolyautoingame",
        description="In-game MLB Poly auto-bet: on, off, or status",
    )
    @app_commands.describe(state="on, off, or status")
    async def slash_mlbpolyautoingame(
        interaction: discord.Interaction, state: str = "status"
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        state = state.lower().strip()
        if state == "status":
            lines = [
                "**`/mlbpolyautoingame` — live-game auto only**",
                "",
                *_ingame_autobet_status_lines(),
                "",
                "Master must be ON (`/mlbpolyautobet`). "
                "Persist: `POLY_MLB_INGAME_ENABLED` on Railway.",
            ]
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return
        if state not in ("on", "off"):
            await interaction.followup.send(
                "Usage: `/mlbpolyautoingame on|off|status`", ephemeral=True
            )
            return
        CONFIG["POLY_MLB_INGAME_ENABLED"] = 1 if state == "on" else 0
        if state == "on":
            mlb_autobet_state.arm_autobet_suppress()
        logger.warning("mlb_poly_discord: POLY_MLB_INGAME_ENABLED toggled to %s", state)
        eff = mlb_poly_ingame_autobet_enabled()
        await interaction.followup.send(
            f"In-game MLB Poly auto-bet: **{state.upper()}** · "
            f"effective (master ∧ switch): **{'ON' if eff else 'OFF'}**. "
            "Resets on deploy unless env set.",
            ephemeral=True,
        )


async def maybe_auto_bet_mlb(signals: list[dict]) -> None:
    """Pregame auto-bet: **model_confidence** gate, ≤N/day, 1 per game (separate from in-game)."""
    if not signals or not mlb_poly_pregame_autobet_enabled():
        return
    if not poly_client.poly_configured():
        return
    if mlb_autobet_state.autobet_suppressed():
        logger.info("maybe_auto_bet_mlb: suppressed (arm grace / startup window)")
        return

    min_conf = float(CONFIG.get("POLY_MLB_AUTO_MIN_MODEL_CONFIDENCE") or 0.65)
    opt_edge = float(CONFIG.get("POLY_MLB_AUTO_MIN_EDGE") or 0.08)
    max_k = float(CONFIG.get("POLY_MLB_AUTO_MAX_KELLY_DOLLARS") or 1.5)
    max_c = int(CONFIG.get("POLY_MLB_AUTO_MAX_CONTRACTS") or 1)
    max_day = int(CONFIG.get("POLY_MLB_AUTO_MAX_BETS_PER_ET_DAY") or 3)
    need_strong = truthy_config_int(CONFIG.get("POLY_MLB_AUTO_REQUIRE_STRONG"))
    max_implied = float(CONFIG.get("POLY_MLB_AUTO_MAX_IMPLIED") or 0.85)
    min_implied = float(CONFIG.get("POLY_MLB_AUTO_MIN_IMPLIED") or 0.15)

    try:
        with open(mlb_autobet_state.mlb_positions_path(), "r", encoding="utf-8") as fh:
            positions = json.load(fh)
        if not isinstance(positions, list):
            positions = []
    except (FileNotFoundError, json.JSONDecodeError):
        positions = []

    if count_pregame_autos_today_et(positions) >= max_day:
        logger.info(
            "maybe_auto_bet_mlb: pregame daily cap (%d / ET day) reached",
            max_day,
        )
        return

    top: dict | None = None
    for sig in signals:
        gid = sig.get("game_id")
        if gid is None:
            continue
        if game_has_pregame_auto_today(positions, int(gid)):
            continue
        if has_open_pregame_auto(positions, int(gid)):
            continue
        if need_strong and str(sig.get("signal_strength", "")).lower() != "strong":
            continue
        if float(sig.get("model_confidence") or 0) < min_conf:
            continue
        if opt_edge > 0 and abs(float(sig.get("edge") or 0)) < opt_edge:
            continue
        imp_check = float(sig.get("implied_prob") or 0.5)
        if imp_check >= max_implied or imp_check <= min_implied:
            logger.info(
                "maybe_auto_bet_mlb: skip extreme market — implied=%.2f gate=[%.2f, %.2f] ticker=%s",
                imp_check,
                min_implied,
                max_implied,
                sig.get("ticker"),
            )
            continue

        cand = dict(sig)
        kc = max(1, int(cand.get("kelly_contracts") or 1))
        cand["kelly_contracts"] = min(kc, max_c)
        side = str(cand.get("recommended_side", "yes")).lower()
        imp = float(cand.get("implied_prob", 0.5))
        unit = imp if side == "yes" else (1.0 - imp)
        if unit <= 0:
            continue

        est = cand["kelly_contracts"] * unit
        if est > max_k:
            max_contracts_for_dollars = int(max_k / unit)
            if max_contracts_for_dollars < 1:
                continue
            cand["kelly_contracts"] = min(cand["kelly_contracts"], max_contracts_for_dollars)
            est = cand["kelly_contracts"] * unit

        cand["kelly_dollars"] = round(min(max_k, est), 2)
        if cand["kelly_dollars"] <= 0:
            continue
        top = cand
        break

    if top is None:
        return

    top["placed_via"] = "auto_mlb"
    try:
        result = await poly_order_manager.place_mlb_trade(
            top, auto_cap_contracts=max_c
        )
        if result.get("success"):
            await send_mlb_poly_auto_bet_alert(top, result.get("order_id"), None)
        else:
            await send_mlb_poly_auto_bet_alert(top, None, result.get("error", "unknown"))
    except Exception as exc:
        logger.exception("maybe_auto_bet_mlb: %s", exc)
        await send_mlb_poly_auto_bet_alert(top, None, str(exc))


async def maybe_auto_bet_mlb_ingame_from_candidates(candidates: list[dict]) -> None:
    """Place at most one in-game auto order (first = strongest live spot)."""
    if not candidates or not mlb_poly_ingame_autobet_enabled():
        return
    if not poly_client.poly_configured():
        return
    if mlb_autobet_state.autobet_suppressed():
        logger.info("maybe_auto_bet_mlb_ingame: suppressed (arm grace / startup window)")
        return

    top = dict(candidates[0])
    top["placed_via"] = "auto_mlb_ingame"
    max_c = int(CONFIG.get("POLY_MLB_INGAME_MAX_CONTRACTS") or 1)
    try:
        result = await poly_order_manager.place_mlb_trade(
            top, auto_cap_contracts=max_c
        )
        if result.get("success"):
            await send_mlb_poly_auto_bet_alert(top, result.get("order_id"), None)
        else:
            await send_mlb_poly_auto_bet_alert(top, None, result.get("error", "unknown"))
    except Exception as exc:
        logger.exception("maybe_auto_bet_mlb_ingame: %s", exc)
        await send_mlb_poly_auto_bet_alert(top, None, str(exc))
