"""
pipeline/discord_bot.py – Discord bot for pavlov-weather-bot (discord.py 2.x).

Provides:
    SignalView          – interactive button view (BET IT / SKIP)
    PolySignalView      – Polymarket US (separate pending file + button ids)
    build_signal_embed  – rich embed formatter for a signal dict
    send_signal         – post a Kalshi signal alert with the interactive view
    send_poly_signal    – post a Polymarket US signal alert
    send_daily_summary  – post an end-of-day P&L summary embed
    run_bot             – coroutine to start the bot
    get_channel         – returns the cached TextChannel object
    get_poly_channel    – Polymarket alert channel (same as main when DISCORD_POLY_ID is 0)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Literal

import discord
import discord.ui
from discord import app_commands

from config import CONFIG, truthy_config_int
import data_paths as dp
from pipeline import order_manager
from polymarket.poly_discord import (
    PolySignalView,
    post_poly_resolution_update,
    register_poly_app_commands,
    send_poly_auto_bet_alert,
    send_poly_signal,
)
from polymarket.mlb_poly_discord import (
    MLBPolySignalView,
    register_mlb_poly_app_commands,
    send_mlb_poly_auto_bet_alert,
    send_mlb_poly_signal,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_POSITIONS_FILE = os.path.join(dp.logs_dir(), "positions.json")
_PENDING_FILE   = os.path.join(dp.data_dir(), "pending_signals.json")


# ---------------------------------------------------------------------------
# Persistent signal store — keyed by Discord message_id so button views
# survive bot restarts (Railway redeploys, crashes, etc).
# ---------------------------------------------------------------------------

def _load_pending() -> dict:
    try:
        with open(_PENDING_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending(data: dict) -> None:
    os.makedirs(os.path.dirname(_PENDING_FILE), exist_ok=True)
    with open(_PENDING_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def _prune_pending(pending: dict, max_age_hours: int = 48) -> dict:
    """Drop entries older than *max_age_hours* (default 48 h)."""
    from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    "pending": 0xFFAA00,   # amber
    "placed":  0x00FF88,   # green
    "skipped": 0x666666,   # grey
}

_MLB_STRONG = 0x00CC66
_MLB_MODERATE = 0xFFAA00

_STATUS_LABELS = {
    "pending": "SIGNAL",
    "placed":  "PLACED ✅",
    "skipped": "SKIPPED",
}


def build_signal_embed(
    signal: dict,
    status: str = "pending",
    order_id: str | None = None,
    *,
    venue: Literal["kalshi", "poly"] = "kalshi",
) -> discord.Embed:
    """Return a rich Discord embed describing a trading signal.

    Args:
        signal:   Signal dict as returned by signal_engine.calculate_edge().
        status:   One of 'pending', 'placed', or 'skipped'.
        order_id: Kalshi order ID string (only present after a successful bet).

    Returns:
        A fully-populated discord.Embed object.
    """
    label = _STATUS_LABELS.get(status, status.upper())
    city  = signal.get("city", "Unknown")
    side  = signal.get("recommended_side", "yes").upper()
    edge  = signal.get("edge", 0)

    embed = discord.Embed(
        title=f"🌡️ {label}  —  {city}",
        color=_STATUS_COLORS.get(status, 0xFFAA00),
    )

    embed.add_field(
        name="Market",
        value=signal.get("market_title", "—"),
        inline=False,
    )

    # Prominent recommended side so users know what BET IT will place.
    side_emoji = "✅" if side == "YES" else "❌"
    embed.add_field(
        name="Recommendation",
        value=f"**{side_emoji} BET {side}**",
        inline=True,
    )

    implied_pct = signal.get("implied_prob", 0) * 100
    price_label = "Polymarket price" if venue == "poly" else "Kalshi price"
    embed.add_field(
        name=price_label,
        value=f"{implied_pct:.0f}¢  ({implied_pct:.0f}% implied)",
        inline=True,
    )
    embed.add_field(
        name="Model confidence",
        value=f"{signal.get('model_prob', 0) * 100:.0f}%",
        inline=True,
    )
    embed.add_field(
        name="Edge",
        value=f"**{edge * 100:+.1f}¢** ({side} has the edge)",
        inline=True,
    )
    embed.add_field(
        name="NWS forecast",
        value=(
            f"{signal.get('nws_predicted', '?')}°F"
            f"  (threshold: {signal.get('threshold_f', '?')}°F)"
        ),
        inline=True,
    )
    embed.add_field(
        name="Station",
        value=signal.get("station", "—"),
        inline=True,
    )
    embed.add_field(
        name="Kelly bet",
        value=(
            f"${signal.get('kelly_dollars', 0):.2f}"
            f"  ({signal.get('kelly_contracts', 0)} contracts)"
        ),
        inline=True,
    )

    if order_id:
        embed.add_field(name="Order ID", value=order_id, inline=False)

    # Second-source / ensemble indicator
    agreement      = signal.get("source_agreement", "nws_only")
    owm_val        = signal.get("owm_predicted")
    hours_left     = signal.get("hours_left", 999)
    ens_members    = signal.get("ensemble_members", 0)
    ens_mean       = signal.get("ensemble_mean")
    ens_spread     = signal.get("ensemble_spread")

    if ens_members >= 10 and ens_mean is not None:
        spread_str = f"\u00b1{ens_spread}\u00b0F" if ens_spread is not None else ""
        source_str = f"Ensemble {ens_members}m \u2022 mean {ens_mean}\u00b0F {spread_str}"
    elif agreement == "agree" and owm_val:
        source_str = f"NWS + OWM agree ({owm_val}\u00b0F) \u2705"
    elif agreement == "close" and owm_val:
        source_str = f"NWS + OWM close ({owm_val}\u00b0F) ~"
    else:
        source_str = "NWS only"
    hrs_str = f"{hours_left:.1f}h to close" if hours_left < 100 else "multi-day"
    embed.add_field(name="Sources", value=source_str, inline=True)
    embed.add_field(name="Time left", value=hrs_str, inline=True)

    strength   = signal.get("signal_strength", "unknown").upper()
    close_time = signal.get("close_time", "\u2014")
    footer_brand = "Polymarket US" if venue == "poly" else "Pavlov Bot"
    embed.set_footer(
        text=f"Signal: {strength}  |  Closes: {close_time}  |  {footer_brand}"
    )

    return embed


def build_mlb_signal_embed(signal: dict, status: str = "pending") -> discord.Embed:
    """Rich embed for MLB Polymarket moneyline signals."""
    away = signal.get("away_team_abbr") or "Away"
    home = signal.get("home_team_abbr") or "Home"
    strength = str(signal.get("signal_strength") or "moderate").lower()
    color = _MLB_STRONG if strength == "strong" else _MLB_MODERATE
    if status == "skipped":
        color = 0x666666
    elif status == "placed":
        color = 0x00FF88

    embed = discord.Embed(
        title=f"⚾ MLB SIGNAL — {away} @ {home}",
        color=color,
    )

    venue = signal.get("venue_name") or "—"
    gt = signal.get("game_time_et") or ""
    if gt and "T" in gt:
        try:
            from datetime import datetime as _dt

            parts = gt.replace("Z", "+00:00")
            d = _dt.fromisoformat(parts)
            gt_show = d.strftime("%I:%M %p").lstrip("0") or d.strftime("%H:%M")
        except Exception:
            gt_show = gt[:16]
    else:
        gt_show = gt or "—"
    embed.add_field(
        name="Game",
        value=f"{venue} · {gt_show} ET",
        inline=False,
    )

    br = signal.get("probability_breakdown") or {}
    hp = br.get("home_pitcher_analysis") or {}
    ap = br.get("away_pitcher_analysis") or {}
    hscore = float(hp.get("final_pitcher_score") or hp.get("pitcher_score") or 0)
    ascore = float(ap.get("final_pitcher_score") or ap.get("pitcher_score") or 0)
    hm = signal.get("home_pitcher_name") or "TBD"
    am = signal.get("away_pitcher_name") or "TBD"
    hera = hp.get("era")
    aera = ap.get("era")
    hdr = hp.get("days_rest")
    adr = ap.get("days_rest")
    embed.add_field(
        name="Home starter",
        value=(
            f"{hm} ({hera if hera is not None else '—'} ERA · "
            f"{hdr if hdr is not None else '—'}d rest · Score: {hscore:.0f}/100)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Away starter",
        value=(
            f"{am} ({aera if aera is not None else '—'} ERA · "
            f"{adr if adr is not None else '—'}d rest · Score: {ascore:.0f}/100)"
        ),
        inline=False,
    )

    hl = signal.get("home_bullpen_label") or (
        (br.get("home_bullpen") or {}).get("label") if isinstance(br.get("home_bullpen"), dict) else "—"
    )
    al = signal.get("away_bullpen_label") or (
        (br.get("away_bullpen") or {}).get("label") if isinstance(br.get("away_bullpen"), dict) else "—"
    )
    embed.add_field(
        name="Bullpens",
        value=f"Home: {hl} | Away: {al}",
        inline=False,
    )

    re = br.get("run_environment") or {}
    rf = float(signal.get("park_run_factor") or re.get("total_factor") or re.get("run_factor") or 1.0)
    cond = str(signal.get("park_conditions") or re.get("conditions") or "—")
    embed.add_field(
        name="Park",
        value=f"{venue} · Run factor: {rf:.2f} · {cond}",
        inline=False,
    )

    travel = signal.get("travel_summary") or "—"
    embed.add_field(name="Travel", value=travel, inline=False)

    yp = float(signal.get("yes_price") or 0)
    embed.add_field(
        name="Polymarket price",
        value=f"{yp:.0f}¢ ({yp:.0f}% implied)",
        inline=True,
    )
    mp = float(signal.get("model_prob") or signal.get("model_yes_prob") or 0)
    embed.add_field(
        name="Model probability",
        value=f"{mp * 100:.1f}%",
        inline=True,
    )
    edge = float(signal.get("edge") or 0)
    embed.add_field(
        name="Edge",
        value=f"+{abs(edge) * 100:.1f}% over market",
        inline=True,
    )
    embed.add_field(
        name="Kelly bet",
        value=(
            f"${float(signal.get('kelly_dollars') or 0):.2f} "
            f"({signal.get('kelly_contracts') or 0} contracts)"
        ),
        inline=False,
    )

    st = strength.upper()
    footer = f"Signal: {st} | Pavlov MLB Bot"
    if status == "placed":
        footer = f"PLACED | {footer}"
    elif status == "skipped":
        footer = f"SKIPPED | {footer}"
    embed.set_footer(text=footer)

    return embed

class SignalView(discord.ui.View):
    """Persistent view attached to each signal embed.

    Survives bot restarts because:
      - timeout=None (Discord won't expire it)
      - buttons have static custom_ids (Discord routes clicks back to us)
      - signal data is stored on disk in pending_signals.json keyed by
        Discord message_id, looked up at click time

    The button label is generic "✅ BET" — the embed itself prominently
    shows whether the recommendation is YES or NO.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    # ------------------------------------------------------------------
    # BET IT
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="✅ BET",
        style=discord.ButtonStyle.success,
        custom_id="pavlov_bet",
    )
    async def bet_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Place the trade and update the message with the result."""
        await interaction.response.defer()

        msg_id  = str(interaction.message.id) if interaction.message else ""
        pending = _load_pending()
        signal  = pending.get(msg_id)

        if not signal:
            await interaction.edit_original_response(
                content="❌ This signal has expired or was already actioned.",
                embed=None, view=None,
            )
            return

        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(
            content="⏳ Placing order…", embed=None, view=self
        )

        from pipeline import kalshi_client as kc
        result = await order_manager.place_trade(signal, kc)

        if result["success"]:
            embed = build_signal_embed(
                signal,
                status="placed",
                order_id=result.get("order_id"),
            )
            await interaction.edit_original_response(
                content="", embed=embed, view=self
            )
            logger.info(
                "DiscordBot: trade placed for %s – order_id=%s",
                signal.get("ticker"),
                result.get("order_id"),
            )
        else:
            await interaction.edit_original_response(
                content=f"❌ Order failed: {result.get('error', 'unknown error')}",
                embed=None,
                view=None,
            )
            logger.error(
                "DiscordBot: trade failed for %s – %s",
                signal.get("ticker"),
                result.get("error"),
            )

        pending.pop(msg_id, None)
        _save_pending(pending)

    # ------------------------------------------------------------------
    # SKIP
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="❌ SKIP",
        style=discord.ButtonStyle.danger,
        custom_id="pavlov_skip",
    )
    async def skip_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Log the skip and update the embed to the skipped state."""
        msg_id  = str(interaction.message.id) if interaction.message else ""
        pending = _load_pending()
        signal  = pending.get(msg_id)

        if not signal:
            await interaction.response.send_message(
                "This signal has expired.", ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True
        order_manager.log_skip(signal)

        embed = build_signal_embed(signal, status="skipped")
        await interaction.response.edit_message(embed=embed, view=self)

        logger.info(
            "DiscordBot: signal skipped for %s.", signal.get("ticker")
        )

        pending.pop(msg_id, None)
        _save_pending(pending)


# ---------------------------------------------------------------------------
# High-level send helpers
# ---------------------------------------------------------------------------

async def send_signal(
    channel: discord.TextChannel,
    signal: dict,
    kalshi_client,
) -> None:
    """Post a signal alert embed with interactive BET / SKIP buttons.

    Stores the signal in pending_signals.json keyed by Discord message_id so
    button clicks survive bot restarts.

    Args:
        channel:       Discord TextChannel to send to.
        signal:        Signal dict from signal_engine.get_all_signals().
        kalshi_client: Kept for backwards compatibility — no longer used here
                       (the persistent view late-imports kalshi_client at
                       click time).
    """
    from datetime import datetime, timezone
    embed = build_signal_embed(signal, status="pending")
    view  = SignalView()
    msg   = await channel.send(embed=embed, view=view)

    # Stamp + persist for the persistent view to pick up later.
    signal_to_store = dict(signal)
    signal_to_store["posted_at"] = datetime.now(timezone.utc).isoformat()

    pending = _prune_pending(_load_pending(), max_age_hours=48)
    pending[str(msg.id)] = signal_to_store
    _save_pending(pending)

    logger.info(
        "DiscordBot: signal sent for %s (edge=%.4f, msg_id=%s).",
        signal.get("ticker"),
        signal.get("edge", 0),
        msg.id,
    )


async def send_mlb_signal(
    channel: discord.abc.Messageable | None,
    signal: dict,
) -> None:
    """Post an MLB Polymarket signal with BET IT / SKIP / BREAKDOWN.

    If *channel* is ``None``, uses :func:`get_mlb_poly_channel` then :func:`get_channel`.
    """
    from polymarket.mlb_poly_discord import send_mlb_poly_signal_to_channel

    ch = channel or get_mlb_poly_channel() or get_channel()
    if ch is None:
        logger.warning("DiscordBot: send_mlb_signal — no channel available")
        return
    await send_mlb_poly_signal_to_channel(ch, signal)


async def send_auto_bet_alert(
    channel: discord.TextChannel,
    signal: dict,
    order_id: str | None,
    error: str | None = None,
) -> None:
    """Post a non-interactive 'AUTO-BET PLACED' (or failed) embed.

    No BET / SKIP buttons — the bot has already acted.  This is purely a
    notification so the user can see what was placed.
    """
    city = signal.get("city", "Unknown")
    side = signal.get("recommended_side", "yes").upper()

    if error:
        embed = discord.Embed(
            title=f"⚠️ AUTO-BET FAILED  —  {city}",
            description=f"```{error}```",
            color=0xFF4444,
        )
    else:
        embed = discord.Embed(
            title=f"🤖 AUTO-BET PLACED  —  {city}",
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
        embed.set_footer(text=f"Order: {order_id}")

    await channel.send(embed=embed)
    logger.info(
        "DiscordBot: auto-bet alert posted for %s (success=%s).",
        signal.get("ticker"), error is None,
    )


async def send_daily_summary(
    channel: discord.TextChannel,
    stats: dict,
) -> None:
    """Post an end-of-day performance summary embed.

    Expected keys in *stats*:
        pl            – net P&L in dollars (float, signed)
        win_rate      – fraction 0–1
        wins          – number of winning trades (int)
        total         – total resolved trades (int)
        bankroll      – current account balance in dollars
        signals_fired – number of signals sent today (int)
        best_city     – name of the top-performing city today (str, optional)
    """
    embed = discord.Embed(title="📊 Daily Summary", color=0x378ADD)

    embed.add_field(
        name="P&L",
        value=f"${stats.get('pl', 0):+.2f}",
        inline=True,
    )
    wins  = stats.get("wins", 0)
    total = stats.get("total", 0)
    embed.add_field(
        name="Win rate",
        value=f"{stats.get('win_rate', 0) * 100:.0f}%  ({wins}/{total})",
        inline=True,
    )
    embed.add_field(
        name="Bankroll",
        value=f"${stats.get('bankroll', 0):.2f}",
        inline=True,
    )
    embed.add_field(
        name="Signals fired",
        value=str(stats.get("signals_fired", 0)),
        inline=True,
    )
    embed.add_field(
        name="Best city",
        value=stats.get("best_city", "—"),
        inline=True,
    )

    await channel.send(embed=embed)
    logger.info("DiscordBot: daily summary posted.")


# ---------------------------------------------------------------------------
# Bot client with slash-command support
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)
register_poly_app_commands(tree)
register_mlb_poly_app_commands(tree)

_channel: discord.TextChannel | None = None
_poly_channel: discord.TextChannel | None = None
_mlb_poly_channel: discord.TextChannel | None = None


@client.event
async def on_interaction(interaction: discord.Interaction) -> None:
    """Log every interaction so we can see slash commands arriving."""
    logger.info(
        "DiscordBot: interaction received type=%s name=%s user=%s",
        interaction.type,
        getattr(interaction, 'command', None) and interaction.command.name,
        interaction.user,
    )


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """Catch unhandled slash-command errors and report them to the user."""
    logger.exception("DiscordBot: unhandled slash-command error — %s", error)
    msg = f"\u274c Command error: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@client.event
async def on_ready() -> None:
    """Cache the target channel once the bot has connected.

    Also registers the persistent SignalView so button clicks on messages
    sent in previous bot runs continue to work after a restart.
    """
    global _channel, _poly_channel, _mlb_poly_channel
    _channel = client.get_channel(int(CONFIG["DISCORD_CHANNEL_ID"]))
    _poly_channel = client.get_channel(
        int(CONFIG["DISCORD_POLY_ID"] or CONFIG["DISCORD_CHANNEL_ID"])
    )
    mlb_id = int(CONFIG.get("DISCORD_POLY_MLB_ID") or 0)
    if mlb_id:
        _mlb_poly_channel = client.get_channel(mlb_id)
    else:
        _mlb_poly_channel = _poly_channel

    # Register persistent view — must be called every restart.  Without this
    # the BET/SKIP buttons on already-posted signals fail with "interaction
    # failed" after the bot redeploys.
    client.add_view(SignalView())
    client.add_view(PolySignalView())
    client.add_view(MLBPolySignalView())

    # Sync slash commands to every guild the bot is in immediately (guild sync
    # propagates in <5 seconds vs up to 1 hour for a global sync).
    guild_ids: list[int] = []
    for guild in client.guilds:
        try:
            await tree.sync(guild=guild)
            guild_ids.append(guild.id)
        except Exception as exc:
            logger.warning("DiscordBot: guild sync failed for %s — %s", guild.id, exc)

    # Also kick off a global sync so commands show up if the bot is added to
    # new servers later.
    try:
        await tree.sync()
    except Exception as exc:
        logger.warning("DiscordBot: global tree.sync() failed — %s", exc)

    logger.info(
        "DiscordBot: logged in as %s (id=%s). Channel: %s. Poly channel: %s. MLB Poly: %s. Slash commands synced to guilds: %s",
        client.user,
        client.user.id if client.user else "?",
        _channel,
        _poly_channel,
        _mlb_poly_channel,
        guild_ids,
    )
    print(
        f"Discord bot ready — channel: {_channel}  |  poly: {_poly_channel}  |  mlb_poly: {_mlb_poly_channel}  |  synced guilds: {guild_ids}"
    )


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@tree.command(name="ping", description="Check if the bot is alive and responding")
async def slash_ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("🏓 Pong — bot is alive!", ephemeral=True)


def _load_positions() -> list[dict]:
    try:
        with open(_POSITIONS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@tree.command(name="status", description="Show bot health: bankroll, open positions, and today\u2019s P&L")
async def slash_status(interaction: discord.Interaction) -> None:
    # Defer immediately — kc.get_account_balance() is a blocking HTTP call that
    # can take >3 s and would otherwise cause "did not respond".
    await interaction.response.defer(ephemeral=True)
    try:
        from pipeline import kalshi_client as kc
        from pipeline import learning_loop
        loop     = asyncio.get_event_loop()
        balance  = await loop.run_in_executor(None, kc.get_account_balance)
        stats    = learning_loop.generate_summary(hours=24)
        local_open = [p for p in _load_positions() if p.get("status") == "open"]
        try:
            api_open = await loop.run_in_executor(None, kc.get_open_positions)
            open_n = len(api_open)
        except Exception as exc:
            logger.warning("DiscordBot: /status — exchange positions unavailable (%s); using bot log.", exc)
            open_n = len(local_open)
        embed = discord.Embed(title="\U0001f916 Pavlov Bot — Status", color=0x378ADD)
        embed.add_field(name="Bankroll",        value=f"${balance:.2f}",                 inline=True)
        embed.add_field(name="Open positions",  value=str(open_n),                       inline=True)
        embed.add_field(name="Today P&L",       value=f"${stats['pl']:+.2f}",            inline=True)
        embed.add_field(name="Today wins",      value=f"{stats['wins']}/{stats['total']}", inline=True)
        embed.add_field(name="Win rate",        value=f"{stats['win_rate']*100:.0f}%",   inline=True)
        embed.set_footer(text="Open count from Kalshi when available | Pavlov Bot")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        logger.exception("DiscordBot: /status handler crashed — %s", exc)
        await interaction.followup.send(f"\u274c Error: `{exc}`", ephemeral=True)


@tree.command(name="positions", description="List all open positions with entry price")
async def slash_positions(interaction: discord.Interaction) -> None:
    """Prefer live Kalshi portfolio (survives Railway redeploys); fall back to bot log."""
    await interaction.response.defer(ephemeral=True)
    try:
        from pipeline import kalshi_client as kc
        loop = asyncio.get_event_loop()
        local_open = [p for p in _load_positions() if p.get("status") == "open"]
        api_open: list[dict] | None = None
        try:
            api_open = await loop.run_in_executor(None, kc.get_open_positions)
        except Exception as exc:
            logger.warning("DiscordBot: /positions — get_open_positions failed — %s", exc)

        if api_open:
            embed = discord.Embed(
                title="\U0001f4cb Open Positions (Kalshi)",
                color=0xFFAA00,
            )
            max_show = 12
            slice_open = api_open[:max_show]
            for p in slice_open:
                net = kc.market_position_net_contracts(p)
                side = "YES" if net > 0 else "NO"
                n = abs(net)
                ticker = p.get("ticker", "?")
                exp = p.get("market_exposure_dollars", 0)
                name, val = kc.format_open_position_embed_field(ticker, side, n, exp)
                embed.add_field(name=name[:256], value=val[:1024], inline=False)
            foot = "Live from Kalshi API · amounts rounded"
            if len(api_open) > max_show:
                foot = f"Showing {max_show} of {len(api_open)} · {foot}"
            embed.set_footer(text=foot)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if not local_open:
            await interaction.followup.send("No open positions.", ephemeral=True)
            return

        embed = discord.Embed(
            title="\U0001f4cb Open Positions (bot log)",
            color=0xFFAA00,
        )
        for p in local_open[:8]:
            side   = p.get("recommended_side", "?").upper()
            price  = p.get("price_cents", "?")
            n      = p.get("kelly_contracts", 1)
            ticker = p.get("ticker", "?")
            embed.add_field(
                name=p.get("city", ticker),
                value=f"{side} {n}\u00d7 @ {price}\u00a2 | {ticker}",
                inline=False,
            )
        embed.set_footer(text="Local logs/positions.json — Kalshi API was unreachable")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        logger.exception("DiscordBot: /positions handler crashed — %s", exc)
        await interaction.followup.send(f"\u274c Error: `{exc}`", ephemeral=True)


@tree.command(name="pnl", description="Show detailed P&L breakdown by city")
async def slash_pnl(interaction: discord.Interaction) -> None:
    positions = [p for p in _load_positions() if p.get("status") in ("won", "lost")]
    by_city: dict[str, float] = {}
    for p in positions:
        city = p.get("city", "Unknown")
        by_city[city] = round(by_city.get(city, 0.0) + p.get("pl", 0.0), 4)
    if not by_city:
        await interaction.response.send_message("No resolved trades yet.", ephemeral=True)
        return
    embed = discord.Embed(title="\U0001f4b0 P&L by City (all time)", color=0x00FF88)
    for city, pl in sorted(by_city.items(), key=lambda x: -abs(x[1])):
        icon = "\u2705" if pl >= 0 else "\u274c"
        embed.add_field(name=city, value=f"{icon} ${pl:+.2f}", inline=True)
    total_pl = sum(by_city.values())
    embed.set_footer(text=f"Total: ${total_pl:+.2f} across {len(positions)} trades")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="autobet", description="Toggle autonomous betting on/off (or check status)")
@app_commands.describe(state="on, off, or status")
async def slash_autobet(interaction: discord.Interaction, state: str = "status") -> None:
    """Runtime toggle for AUTO_BET_ENABLED without redeploying."""
    try:
        state = state.lower().strip()

        if state == "status":
            enabled    = truthy_config_int(CONFIG.get("AUTO_BET_ENABLED"))
            max_n      = CONFIG.get("AUTO_BET_MAX_PER_DAY") or 6
            max_d      = float(CONFIG.get("AUTO_BET_MAX_DOLLARS_PER_DAY") or 10.0)
            min_e      = float(CONFIG.get("AUTO_BET_MIN_EDGE") or 0.35)
            max_s      = float(CONFIG.get("AUTO_BET_MAX_SPREAD") or 1.8)
            min_m      = float(CONFIG.get("AUTO_BET_MIN_MARGIN") or 2.5)
            min_p_yes  = float(CONFIG.get("AUTO_BET_MIN_PROB_YES") or 0.95)
            max_p_no   = float(CONFIG.get("AUTO_BET_MAX_PROB_NO") or 0.05)
            buf_cents  = int(CONFIG.get("AUTO_BET_PRICE_BUFFER_CENTS") or 5)
            embed = discord.Embed(
                title="\U0001f916 Auto-Bet Status",
                color=0x00BBFF if enabled else 0x666666,
            )
            embed.add_field(name="Enabled",       value="\u2705 YES" if enabled else "\u274c NO", inline=True)
            embed.add_field(name="Max bets/day",  value=str(max_n),                inline=True)
            embed.add_field(name="Max spend/day", value=f"${max_d:.2f}",           inline=True)
            embed.add_field(name="Min edge",      value=f"{min_e * 100:.0f}\u00a2", inline=True)
            embed.add_field(name="Max spread",    value=f"{max_s:.1f}\u00b0F",     inline=True)
            embed.add_field(name="Min margin",    value=f"{min_m:.1f}\u00b0F",     inline=True)
            embed.add_field(name="Min YES prob",  value=f"{min_p_yes * 100:.0f}%", inline=True)
            embed.add_field(name="Max NO prob",   value=f"{max_p_no * 100:.0f}%",  inline=True)
            embed.add_field(name="Limit +buffer", value=f"+{buf_cents}\u00a2 vs ask", inline=True)
            embed.add_field(name="Dedup",         value="1 bet / city / market day", inline=True)
            embed.set_footer(
                text="Kalshi only — Polymarket US uses /polyautobet; set POLY_AUTO_BET_ENABLED on the host."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if state not in ("on", "off"):
            await interaction.response.send_message(
                "Usage: `/autobet on`, `/autobet off`, or `/autobet status`",
                ephemeral=True,
            )
            return

        CONFIG["AUTO_BET_ENABLED"] = 1 if state == "on" else 0
        logger.warning("DiscordBot: AUTO_BET_ENABLED toggled %s by user.", state.upper())
        icon = "\U0001f7e2" if state == "on" else "\U0001f534"
        await interaction.response.send_message(
            f"{icon} Auto-bet is now **{state.upper()}**.\n"
            f"This resets on Railway restart — set `AUTO_BET_ENABLED=1` in env vars to persist.",
            ephemeral=True,
        )

    except Exception as exc:
        logger.exception("DiscordBot: /autobet handler crashed — %s", exc)
        try:
            await interaction.response.send_message(
                f"\u274c Error in /autobet: `{exc}`", ephemeral=True
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Resolution update — edits original embed when a market settles
# ---------------------------------------------------------------------------

async def post_resolution_update(
    channel: discord.TextChannel,
    position: dict,
) -> None:
    """Post a follow-up message in channel when a position resolves.

    Sends a compact win/loss notification rather than trying to edit the
    original embed (message IDs aren’t tracked across restarts).
    """
    won   = position.get("status") == "won"
    pl    = position.get("pl", 0.0)
    city  = position.get("city", position.get("ticker", "?"))
    side  = position.get("recommended_side", "?").upper()
    color = 0x00FF88 if won else 0xFF4444
    icon  = "\U0001f7e2" if won else "\U0001f534"
    embed = discord.Embed(
        title=f"{icon} {'WIN' if won else 'LOSS'} — {city}",
        description=(
            f"**{side}** bet settled {'in your favour' if won else 'against you'}.\n"
            f"P&L: **${pl:+.2f}**"
        ),
        color=color,
    )
    embed.add_field(name="Ticker",   value=position.get("ticker", "?"),  inline=True)
    embed.add_field(name="Contracts", value=str(position.get("kelly_contracts", 1)), inline=True)
    embed.add_field(name="Entry",     value=f"{position.get('price_cents','?')}\u00a2", inline=True)
    foot = f"Resolved at {position.get('resolved_at', '')}"
    if position.get("venue") == "poly_us":
        foot += " | Polymarket US"
    else:
        foot += " | Pavlov Bot"
    embed.set_footer(text=foot)
    await channel.send(embed=embed)
    logger.info("DiscordBot: resolution posted for %s (%s $%.2f)",
                position.get("ticker"), "WIN" if won else "LOSS", pl)


async def run_bot() -> None:
    """Start the Discord client.  Awaitable; run inside an asyncio event loop."""
    await client.start(CONFIG["DISCORD_BOT_TOKEN"])


def get_channel() -> discord.TextChannel | None:
    """Return the cached TextChannel, or None if the bot is not yet ready."""
    return _channel


def get_poly_channel() -> discord.TextChannel | None:
    """Cached Polymarket alerts channel; mirrors main when ``DISCORD_POLY_ID`` is 0."""
    return _poly_channel


def get_mlb_poly_channel() -> discord.TextChannel | None:
    """MLB Polymarket channel; uses ``DISCORD_POLY_MLB_ID`` or falls back to poly/main."""
    return _mlb_poly_channel
