"""
main.py – Orchestrator and CLI entry point for pavlov-weather-bot.

Usage
-----
  python main.py run
      Start the Discord bot and the scheduler loop (production mode).

  python main.py test
      Run one pipeline cycle, print signals to stdout, no Discord messages.

  python main.py status
      Print current bankroll, open positions, and station scores table.

  python main.py simulate CITY METRIC THRESHOLD
      Print the full edge calculation for custom parameters without placing
      any trades or sending Discord messages.
      Example: python main.py simulate Chicago high 85
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Logging – configure before any pipeline imports so module loggers pick it up.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from config import CONFIG, truthy_config_int
import data_paths as dp
from pipeline import kalshi_client as kc
from pipeline import discord_bot, learning_loop, order_manager, signal_engine
from pipeline.station_mapper import STATION_MAP
from polymarket import paths as poly_paths
from polymarket import poly_client
from polymarket import poly_learning_loop
from polymarket import poly_order_manager
from polymarket import poly_signal_engine

_POSITIONS_FILE = os.path.join(dp.logs_dir(), "positions.json")
_SCORES_FILE    = os.path.join(dp.logs_dir(), "station_scores.json")


# ---------------------------------------------------------------------------
# JSON helper
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _auto_bet_ledger_path() -> str:
    return os.path.join(dp.logs_dir(), "auto_bet_ledger.jsonl")


def _append_auto_bet_ledger(entry: dict) -> None:
    path = _auto_bet_ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def _iter_auto_bet_ledger() -> list[dict]:
    path = _auto_bet_ledger_path()
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return out


def _auto_bet_city_date_in_ledger(city: str, market_date: str, venue: str) -> bool:
    if not city or not market_date:
        return False
    cty = city.lower()
    for rec in _iter_auto_bet_ledger():
        if rec.get("venue") != venue:
            continue
        if (rec.get("city") or "").lower() == cty and rec.get("market_date") == market_date:
            return True
    return False


def _ledger_auto_bet_ticker_today(ticker: str, venue: str) -> bool:
    """True if today's UTC ledger already records an auto-bet on this ticker."""
    if not ticker:
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for rec in _iter_auto_bet_ledger():
        if rec.get("venue") != venue or rec.get("ticker") != ticker:
            continue
        if str(rec.get("placed_at", "")).startswith(today):
            return True
    return False


def _sync_kalshi_exchange_dedup(
    weather_by_ticker: dict[str, dict],
) -> tuple[set[tuple[str, str]], set[str]]:
    """Open Kalshi positions as (city_lower, measurement_date) pairs and ticker set.

    Ticker-level membership is the strongest guard (survives parse failures). City
    pairs still help when we already hold a different strike for the same city/day.
    """
    city_dates: set[tuple[str, str]] = set()
    tickers: set[str] = set()
    try:
        rows = kc.get_open_positions()
    except Exception as exc:
        logger.warning("main: exchange dedup — get_open_positions failed — %s", exc)
        return city_dates, tickers
    for row in rows:
        tick = row.get("ticker")
        if not tick:
            continue
        tickers.add(tick)
        m = weather_by_ticker.get(tick)
        if m is None:
            m = kc.get_market_as_parsed(tick)
        if not m:
            continue
        parsed = signal_engine.parse_market(m)
        if not parsed:
            continue
        md = signal_engine.measurement_date_str(m)
        city_dates.add((parsed["city"].lower(), md))
    return city_dates, tickers


def _kalshi_auto_bet_blocked(
    sig: dict,
    *,
    exchange_cd: set[tuple[str, str]],
    exchange_tickers: set[str],
) -> str | None:
    """Return a reason string if this Kalshi auto-bet should be skipped, else None."""
    ticker = (sig.get("ticker") or "").strip()
    city = (sig.get("city") or "").strip()
    market_date = (sig.get("market_date") or "").strip()
    target_cd = (city.lower(), market_date) if city and market_date else None

    if ticker and ticker in exchange_tickers:
        return f"already hold {ticker} on Kalshi (exchange)"

    if target_cd and target_cd in exchange_cd:
        return f"already have Kalshi exposure for {city} on {market_date} (exchange)"

    if _auto_bet_city_date_in_ledger(city, market_date, "kalshi"):
        return f"auto-bet ledger already has {city} {market_date}"

    if ticker and _ledger_auto_bet_ticker_today(ticker, "kalshi"):
        return f"auto-bet ledger already logged {ticker} today"

    pos_path = os.path.join(dp.logs_dir(), "positions.json")
    for p in _load_json(pos_path, []):
        if p.get("venue") == "poly_us":
            continue
        if p.get("status") != "open":
            continue
        if ticker and p.get("ticker") == ticker:
            return f"local log has open position for {ticker}"
        if (
            target_cd
            and (p.get("city") or "").lower() == target_cd[0]
            and (p.get("market_date") or "") == target_cd[1]
        ):
            return f"local log already has open stake for {city} {market_date}"
    return None


def _auto_bet_today_stats() -> tuple[int, float]:
    """Return (count, dollars_spent) for Kalshi auto-bets placed today (UTC).

    Merges ``logs/positions.json`` with ``auto_bet_ledger.jsonl`` so daily caps
    survive redeploys (deduped per ticker).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_ticker: dict[str, float] = {}
    positions = _load_json(os.path.join(dp.logs_dir(), "positions.json"), [])
    for p in positions:
        if p.get("placed_via") != "auto":
            continue
        if p.get("venue") == "poly_us":
            continue
        placed = str(p.get("placed_at", ""))
        if not placed.startswith(today):
            continue
        t = p.get("ticker") or ""
        if not t:
            continue
        price = float(p.get("price_cents") or 0)
        contracts = float(p.get("kelly_contracts") or 0)
        cost = price / 100.0 * contracts
        by_ticker[t] = max(by_ticker.get(t, 0.0), cost)
    for rec in _iter_auto_bet_ledger():
        if rec.get("venue") != "kalshi":
            continue
        placed = str(rec.get("placed_at", ""))
        if not placed.startswith(today):
            continue
        t = rec.get("ticker") or ""
        if not t:
            continue
        cost = float(rec.get("kelly_dollars") or 0)
        by_ticker[t] = max(by_ticker.get(t, 0.0), cost)
    return len(by_ticker), round(sum(by_ticker.values()), 2)


def _city_date_taken(
    city: str,
    market_date: str,
    *,
    exchange_cd: set[tuple[str, str]] | None = None,
) -> bool:
    """Return True if we already have *any* Kalshi stake on (city, market_date).

    Checks: local positions, auto-bet ledger (survives redeploy), and open
    Kalshi exchange positions mapped to measurement days.
    """
    if not city or not market_date:
        return False
    target = (city.lower(), market_date)
    if exchange_cd and target in exchange_cd:
        return True
    if _auto_bet_city_date_in_ledger(city, market_date, "kalshi"):
        return True
    _positions_path = os.path.join(dp.logs_dir(), "positions.json")
    positions = _load_json(_positions_path, [])
    for p in positions:
        if p.get("venue") == "poly_us":
            continue
        if (p.get("city", "").lower(), p.get("market_date", "")) == target:
            return True
    return False


def _poly_auto_bet_today_stats() -> tuple[int, float]:
    """Return (count, dollars_spent) for Polymarket auto-bets placed today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_ticker: dict[str, float] = {}
    positions = _load_json(poly_paths.POSITIONS, [])
    for p in positions:
        if p.get("placed_via") != "auto":
            continue
        placed = str(p.get("placed_at", ""))
        if not placed.startswith(today):
            continue
        t = p.get("ticker") or ""
        if not t:
            continue
        price = float(p.get("price_cents") or 0)
        contracts = float(p.get("kelly_contracts") or 0)
        cost = price / 100.0 * contracts
        by_ticker[t] = max(by_ticker.get(t, 0.0), cost)
    for rec in _iter_auto_bet_ledger():
        if rec.get("venue") != "poly_us":
            continue
        placed = str(rec.get("placed_at", ""))
        if not placed.startswith(today):
            continue
        t = rec.get("ticker") or ""
        if not t:
            continue
        cost = float(rec.get("kelly_dollars") or 0)
        by_ticker[t] = max(by_ticker.get(t, 0.0), cost)
    return len(by_ticker), round(sum(by_ticker.values()), 2)


def _poly_city_date_taken(
    city: str,
    market_date: str,
    *,
    ticker: str | None = None,
) -> bool:
    """True if any Polymarket position or prior poly auto-bet exists for (city, market_date)."""
    tick = (ticker or "").strip()
    if tick and _ledger_auto_bet_ticker_today(tick, "poly_us"):
        return True
    if not city or not market_date:
        if tick:
            positions = _load_json(poly_paths.POSITIONS, [])
            for p in positions:
                if p.get("status") == "open" and p.get("ticker") == tick:
                    return True
        return False
    if _auto_bet_city_date_in_ledger(city, market_date, "poly_us"):
        return True
    positions = _load_json(poly_paths.POSITIONS, [])
    target = (city.lower(), market_date)
    for p in positions:
        if tick and p.get("ticker") == tick:
            return True
        if (p.get("city", "").lower(), p.get("market_date", "")) == target:
            return True
    return False


# ---------------------------------------------------------------------------
# Core pipeline cycle
# ---------------------------------------------------------------------------

# Tracks signals already posted to Discord this trading day (ticker+side).
# Resets at midnight so tomorrow's markets always get a fresh ping.
_sent_signals: set[str] = set()
_sent_signals_date: str = ""  # YYYY-MM-DD of last reset

_poly_sent_signals: set[str] = set()
_poly_sent_signals_date: str = ""
_poly_skip_logged: bool = False


async def run_cycle() -> None:
    """One full scan → signal → alert cycle."""
    global _sent_signals, _sent_signals_date
    logger.info("── run_cycle starting ──")

    # Reset the dedup set each calendar day so new-day markets always fire.
    today = datetime.now().strftime("%Y-%m-%d")
    if today != _sent_signals_date:
        _sent_signals = set()
        _sent_signals_date = today
        logger.info("run_cycle: dedup set reset for %s.", today)

    loop = asyncio.get_event_loop()

    # All of these are synchronous blocking HTTP calls (requests library).
    # Run them in a thread executor so the event loop stays free for Discord
    # slash-command interactions, button clicks, and other async tasks.
    markets = await loop.run_in_executor(None, kc.get_weather_markets)
    logger.info("run_cycle: %d weather markets fetched.", len(markets))

    weather_by_ticker = {m["ticker"]: m for m in markets if m.get("ticker")}
    exchange_cd, exchange_tickers = await loop.run_in_executor(
        None,
        _sync_kalshi_exchange_dedup,
        weather_by_ticker,
    )
    logger.info(
        "run_cycle: Kalshi exchange dedup — %d open tickers, %d (city,date) pairs.",
        len(exchange_tickers),
        len(exchange_cd),
    )

    balance = await loop.run_in_executor(None, kc.get_account_balance)
    logger.info("run_cycle: bankroll = $%.2f", balance)

    # Daily loss limit guard.
    max_loss = CONFIG.get("MAX_DAILY_LOSS", 5.0)
    daily_stats = learning_loop.generate_summary(hours=24)
    daily_pl = daily_stats.get("pl", 0.0)
    if daily_pl < -abs(max_loss):
        msg = f"Daily loss limit hit (${daily_pl:.2f} today). Trading paused."
        logger.warning("run_cycle: %s", msg)
        ch = discord_bot.get_channel()
        if ch:
            await ch.send(f"\u26a0\ufe0f **Pavlov Bot \u2014 {msg}**")
        logger.info("\u2500\u2500 run_cycle complete (paused) \u2500\u2500")
        return

    # get_all_signals triggers NWS + METAR + ensemble + OWM fetches — all blocking.
    signals = await loop.run_in_executor(
        None, signal_engine.get_all_signals, markets, balance
    )
    logger.info("run_cycle: %d actionable signals.", len(signals))

    from pipeline import webhook_alerts
    use_webhook = webhook_alerts.webhook_enabled()
    ch = discord_bot.get_channel()
    if ch is None and not use_webhook:
        logger.warning("run_cycle: Discord channel not available yet.")
    elif ch is not None or use_webhook:
        # ── Auto-bet eligibility (positions + ledger + exchange) ──
        auto_bet_enabled  = truthy_config_int(CONFIG.get("AUTO_BET_ENABLED"))
        max_per_day       = int(CONFIG.get("AUTO_BET_MAX_PER_DAY") or 8)
        max_dollars_day   = float(CONFIG.get("AUTO_BET_MAX_DOLLARS_PER_DAY") or 15.00)
        auto_count_today, auto_spend_today = _auto_bet_today_stats()
        logger.info(
            "run_cycle: auto-bet %s — today so far: %d trades / $%.2f spent.",
            "ON" if auto_bet_enabled else "OFF",
            auto_count_today, auto_spend_today,
        )

        new_count = 0
        for sig in signals:
            sig_key = f"{sig['ticker']}:{sig['recommended_side']}"
            if sig_key in _sent_signals:
                logger.debug("run_cycle: skipping already-sent signal %s", sig_key)
                continue

            # Try auto-bet first; if it places, skip the interactive alert.
            auto_placed = False
            watch_reason: str | None = None

            if auto_bet_enabled:
                if auto_count_today >= max_per_day:
                    watch_reason = f"auto_bet: daily trade cap ({max_per_day})"
                    logger.info(
                        "run_cycle: daily cap reached (%d), skipping auto-bet for %s",
                        auto_count_today,
                        sig["ticker"],
                    )
                else:
                    ok, reason = signal_engine.should_auto_bet(sig)
                    if not ok:
                        watch_reason = f"auto_bet: {reason}"
                        logger.info(
                            "run_cycle: auto-bet REJECTED %s — %s",
                            sig["ticker"],
                            reason,
                        )
                    else:
                        block = _kalshi_auto_bet_blocked(
                            sig,
                            exchange_cd=exchange_cd,
                            exchange_tickers=exchange_tickers,
                        )
                        if block:
                            watch_reason = f"auto_bet: {block}"
                            logger.info(
                                "run_cycle: auto-bet REJECTED %s — %s",
                                sig["ticker"],
                                block,
                            )
                        else:
                            cost = float(sig.get("kelly_dollars") or 0)
                            if auto_spend_today + cost > max_dollars_day:
                                watch_reason = (
                                    "auto_bet: daily dollar cap "
                                    f"(${auto_spend_today:.2f}+${cost:.2f}>${max_dollars_day:.2f})"
                                )
                                logger.info(
                                    "run_cycle: auto-bet blocked for %s — would exceed "
                                    "daily $ cap ($%.2f + $%.2f > $%.2f).",
                                    sig["ticker"],
                                    auto_spend_today,
                                    cost,
                                    max_dollars_day,
                                )
                            else:
                                logger.info(
                                    "run_cycle: auto-bet ELIGIBLE for %s (%s) — placing order.",
                                    sig["ticker"],
                                    reason,
                                )
                                sig["placed_via"] = "auto"
                                result = await order_manager.place_trade(sig, kc)
                                if result.get("success"):
                                    auto_count_today += 1
                                    auto_spend_today += cost
                                    _append_auto_bet_ledger(
                                        {
                                            "venue": "kalshi",
                                            "ticker": sig["ticker"],
                                            "city": sig.get("city", ""),
                                            "market_date": sig.get("market_date", ""),
                                            "placed_at": datetime.now(timezone.utc).isoformat(),
                                            "kelly_dollars": float(sig.get("kelly_dollars") or 0),
                                        }
                                    )
                                    await discord_bot.send_auto_bet_alert(
                                        ch, sig, order_id=result.get("order_id"),
                                    )
                                    auto_placed = True
                                else:
                                    err = result.get("error", "unknown error")
                                    watch_reason = f"order_failed: {err}"
                                    await discord_bot.send_auto_bet_alert(
                                        ch, sig, order_id=None,
                                        error=err,
                                    )
            else:
                logger.debug("run_cycle: auto-bet OFF, skipping %s", sig["ticker"])

            if auto_bet_enabled and not auto_placed and watch_reason:
                order_manager.log_signal_watch(sig, watch_reason)

            if not auto_placed and not auto_bet_enabled:
                await discord_bot.send_signal(ch, sig, kc)
            _sent_signals.add(sig_key)
            new_count += 1
        if new_count == 0:
            logger.info("run_cycle: no new signals to post (all already sent today).")

    resolved = await loop.run_in_executor(None, learning_loop.check_and_resolve, kc)
    if resolved:
        logger.info("run_cycle: %d positions resolved.", len(resolved))
        ch = discord_bot.get_channel()
        if ch:
            for pos in resolved:
                try:
                    await discord_bot.post_resolution_update(ch, pos)
                except Exception as exc:
                    logger.warning("run_cycle: resolution post failed – %s", exc)

    logger.info("── run_cycle complete ──")


async def poly_run_cycle() -> None:
    """Polymarket US scan → signal → alert cycle (isolated state files)."""
    global _poly_sent_signals, _poly_sent_signals_date, _poly_skip_logged
    if not poly_client.poly_configured():
        if not _poly_skip_logged:
            logger.info(
                "poly_run_cycle: Polymarket US disabled — set POLY_KEY_ID and "
                "POLY_SECRET_KEY (or POLYMARKET_* aliases) to enable."
            )
            _poly_skip_logged = True
        return
    _poly_skip_logged = False

    logger.info("── poly_run_cycle starting ──")

    today = datetime.now().strftime("%Y-%m-%d")
    if today != _poly_sent_signals_date:
        _poly_sent_signals = set()
        _poly_sent_signals_date = today
        logger.info("poly_run_cycle: dedup set reset for %s.", today)

    loop = asyncio.get_event_loop()

    markets = await loop.run_in_executor(None, poly_client.get_weather_markets)
    logger.info("poly_run_cycle: %d weather markets fetched.", len(markets))

    balance = await loop.run_in_executor(None, poly_client.get_account_balance)
    logger.info("poly_run_cycle: bankroll = $%.2f", balance)

    max_loss = CONFIG.get("MAX_DAILY_LOSS", 5.0)
    daily_stats = poly_learning_loop.generate_summary(hours=24)
    daily_pl = daily_stats.get("pl", 0.0)
    if daily_pl < -abs(max_loss):
        msg = (
            f"Daily loss limit hit on Polymarket US (${daily_pl:.2f} today). "
            "Poly trading paused."
        )
        logger.warning("poly_run_cycle: %s", msg)
        poly_ch = discord_bot.get_poly_channel() or discord_bot.get_channel()
        if poly_ch:
            await poly_ch.send(f"\u26a0\ufe0f **Pavlov Bot \u2014 {msg}**")
        logger.info("\u2500\u2500 poly_run_cycle complete (paused) \u2500\u2500")
        return

    signals = await loop.run_in_executor(
        None, poly_signal_engine.get_all_signals, markets, balance
    )
    logger.info("poly_run_cycle: %d actionable signals.", len(signals))

    from pipeline import webhook_alerts
    use_webhook = webhook_alerts.webhook_enabled()
    poly_ch = discord_bot.get_poly_channel() or discord_bot.get_channel()
    if poly_ch is None and not use_webhook:
        logger.warning("poly_run_cycle: Discord channel not available yet.")
    elif poly_ch is not None or use_webhook:
        auto_bet_enabled = truthy_config_int(CONFIG.get("POLY_AUTO_BET_ENABLED"))
        max_per_day = int(CONFIG.get("AUTO_BET_MAX_PER_DAY") or 8)
        max_dollars_day = float(CONFIG.get("AUTO_BET_MAX_DOLLARS_PER_DAY") or 15.00)
        auto_count_today, auto_spend_today = _poly_auto_bet_today_stats()
        logger.info(
            "poly_run_cycle: poly auto-bet %s (POLY_AUTO_BET_ENABLED=%r env may override on restart) "
            "— today so far: %d trades / $%.2f spent.",
            "ON" if auto_bet_enabled else "OFF",
            CONFIG.get("POLY_AUTO_BET_ENABLED"),
            auto_count_today,
            auto_spend_today,
        )

        new_count = 0
        for sig in signals:
            sig_key = f"{sig['ticker']}:{sig['recommended_side']}"
            if sig_key in _poly_sent_signals:
                logger.debug("poly_run_cycle: skipping already-sent signal %s", sig_key)
                continue

            auto_placed = False
            watch_reason: str | None = None

            if auto_bet_enabled:
                if auto_count_today >= max_per_day:
                    watch_reason = f"auto_bet: daily trade cap ({max_per_day})"
                    logger.info(
                        "poly_run_cycle: daily cap reached (%d), skipping poly auto-bet for %s",
                        auto_count_today,
                        sig["ticker"],
                    )
                else:
                    ok, reason = signal_engine.should_auto_bet(sig)
                    if ok and _poly_city_date_taken(
                        sig.get("city", ""),
                        sig.get("market_date", ""),
                        ticker=sig.get("ticker"),
                    ):
                        ok = False
                        reason = (
                            f"city+date already taken ({sig.get('city')} {sig.get('market_date')})"
                        )
                    if not ok:
                        watch_reason = f"auto_bet: {reason}"
                        logger.info(
                            "poly_run_cycle: poly auto-bet REJECTED %s — %s",
                            sig["ticker"],
                            reason,
                        )
                    else:
                        cost = float(sig.get("kelly_dollars") or 0)
                        if auto_spend_today + cost > max_dollars_day:
                            watch_reason = (
                                "auto_bet: daily dollar cap "
                                f"(${auto_spend_today:.2f}+${cost:.2f}>${max_dollars_day:.2f})"
                            )
                            logger.info(
                                "poly_run_cycle: poly auto-bet blocked for %s — would exceed "
                                "daily $ cap ($%.2f + $%.2f > $%.2f).",
                                sig["ticker"],
                                auto_spend_today,
                                cost,
                                max_dollars_day,
                            )
                        else:
                            logger.info(
                                "poly_run_cycle: poly auto-bet ELIGIBLE for %s (%s) — placing order.",
                                sig["ticker"],
                                reason,
                            )
                            sig["placed_via"] = "auto"
                            result = await poly_order_manager.place_trade(sig)
                            if result.get("success"):
                                auto_count_today += 1
                                auto_spend_today += cost
                                _append_auto_bet_ledger(
                                    {
                                        "venue": "poly_us",
                                        "ticker": sig["ticker"],
                                        "city": sig.get("city", ""),
                                        "market_date": sig.get("market_date", ""),
                                        "placed_at": datetime.now(timezone.utc).isoformat(),
                                        "kelly_dollars": float(sig.get("kelly_dollars") or 0),
                                    }
                                )
                                await discord_bot.send_poly_auto_bet_alert(
                                    sig, order_id=result.get("order_id"),
                                )
                                auto_placed = True
                            else:
                                err = result.get("error", "unknown error")
                                watch_reason = f"order_failed: {err}"
                                await discord_bot.send_poly_auto_bet_alert(
                                    sig,
                                    order_id=None,
                                    error=err,
                                )
            else:
                logger.debug("poly_run_cycle: poly auto-bet OFF, skipping %s", sig["ticker"])

            if auto_bet_enabled and not auto_placed and watch_reason:
                poly_order_manager.log_signal_watch(sig, watch_reason)

            if not auto_placed and not auto_bet_enabled:
                await discord_bot.send_poly_signal(sig)
            _poly_sent_signals.add(sig_key)
            new_count += 1
        if new_count == 0:
            logger.info(
                "poly_run_cycle: no new signals to post (all already sent today)."
            )

    resolved = await loop.run_in_executor(None, poly_learning_loop.check_and_resolve)
    if resolved:
        logger.info("poly_run_cycle: %d positions resolved.", len(resolved))
        for pos in resolved:
            try:
                await discord_bot.post_poly_resolution_update(pos)
            except Exception as exc:
                logger.warning("poly_run_cycle: resolution post failed – %s", exc)

    logger.info("── poly_run_cycle complete ──")


# ---------------------------------------------------------------------------
# Daily summary (called once per 24 h)
# ---------------------------------------------------------------------------

async def daily_summary() -> None:
    """Post an end-of-day summary to Discord."""
    stats = learning_loop.generate_summary(hours=24)
    ch = discord_bot.get_channel()
    if ch:
        await discord_bot.send_daily_summary(ch, stats)
    logger.info("daily_summary: posted – P&L=$%.2f wins=%d/%d",
                stats["pl"], stats["wins"], stats["total"])


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

async def scheduler_loop() -> None:
    """Run the pipeline every CHECK_INTERVAL_MINUTES and post a daily summary
    once per 24-hour period.

    Waits for the Discord channel to be ready before the first cycle so
    signals aren't silently dropped during the bot's login handshake.
    """
    interval_secs         = CONFIG["CHECK_INTERVAL_MINUTES"] * 60
    summary_every         = 24 * 3600
    elapsed_since_summary = 0

    # Wait for Discord to finish connecting (up to 30 s).
    for _ in range(30):
        if discord_bot.get_channel() is not None:
            break
        await asyncio.sleep(1)
    else:
        logger.warning("scheduler_loop: Discord channel not ready after 30 s — continuing anyway.")

    while True:
        await run_cycle()
        elapsed_since_summary += interval_secs
        if elapsed_since_summary >= summary_every:
            await daily_summary()
            elapsed_since_summary = 0
        await asyncio.sleep(interval_secs)


async def poly_scheduler_loop() -> None:
    """Run the Polymarket pipeline on the same interval; no-op if not configured."""
    interval_secs = CONFIG["CHECK_INTERVAL_MINUTES"] * 60

    for _ in range(30):
        if discord_bot.get_channel() is not None:
            break
        await asyncio.sleep(1)
    else:
        logger.warning(
            "poly_scheduler_loop: Discord channel not ready after 30 s — continuing anyway."
        )

    while True:
        if poly_client.poly_configured():
            try:
                await poly_run_cycle()
            except Exception as exc:
                logger.exception("poly_scheduler_loop: poly_run_cycle failed — %s", exc)
        await asyncio.sleep(interval_secs)


# ---------------------------------------------------------------------------
# Production entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Start the Discord bot and the scheduler loop concurrently."""
    logger.info(
        "pavlov-weather-bot starting – interval=%dm  min_edge=%.2f  kelly=%.2f",
        CONFIG["CHECK_INTERVAL_MINUTES"],
        CONFIG["MIN_EDGE_THRESHOLD"],
        CONFIG["KELLY_FRACTION"],
    )
    sr = dp.state_root()
    if sr != dp.app_root():
        logger.info("STATE_DIRECTORY active — persistent logs/data at %s", sr)
    else:
        dp.warn_if_learning_state_ephemeral(logger)
    await asyncio.gather(
        discord_bot.run_bot(),
        scheduler_loop(),
        poly_scheduler_loop(),
    )



# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_run(_args) -> None:
    """python main.py run – production mode."""
    asyncio.run(main())


def cmd_test(_args) -> None:
    """python main.py test – one cycle, stdout only, no Discord sends."""
    print("\n=== PAVLOV-WEATHER-BOT – TEST CYCLE ===\n")

    markets = kc.get_weather_markets()
    print(f"Weather markets fetched: {len(markets)}")

    balance = kc.get_account_balance()
    print(f"Account balance:         ${balance:.2f}\n")

    signals = signal_engine.get_all_signals(markets, balance)
    print(f"Signals found:           {len(signals)}\n")

    if not signals:
        print("(no signals above threshold)\n")
        return

    for i, sig in enumerate(signals, 1):
        dir_label = {
            "above":    f"above {sig['threshold_f']}°F",
            "below":    f"below {sig['threshold_f']}°F",
            "in_range": f"{sig.get('threshold_lo', '?')}-{sig.get('threshold_hi', '?')}°F range",
        }.get(sig['direction'], sig['direction'])
        print(
            f"  [{i}] {sig['city']} | {sig['metric'].upper()} {dir_label}\n"
            f"      NWS: {sig['nws_predicted']}°F  "
            f"margin: {sig['margin_f']}°F\n"
            f"      Implied: {sig['implied_prob']*100:.0f}¢  "
            f"Model: {sig['model_prob']*100:.0f}%  "
            f"Edge: {sig['edge']*100:+.1f}¢  "
            f"[{sig['signal_strength'].upper()}]\n"
            f"      Side: {sig['recommended_side'].upper()}  "
            f"Kelly: ${sig['kelly_dollars']:.2f}  "
            f"({sig['kelly_contracts']} contracts)\n"
            f"      Ticker: {sig['ticker']}\n"
        )


def cmd_status(_args) -> None:
    """python main.py status – print bankroll, positions, scores."""
    print("\n=== PAVLOV-WEATHER-BOT – STATUS ===\n")

    # Bankroll
    try:
        balance = kc.get_account_balance()
        print(f"💰  Bankroll:  ${balance:.2f}\n")
    except Exception as exc:
        print(f"💰  Bankroll:  (error: {exc})\n")

    # Open positions — prefer live Kalshi portfolio (Railway resets local JSON).
    try:
        api_open = kc.get_open_positions()
    except Exception as exc:
        print(f"📋  Open positions: (Kalshi API error: {exc})\n")
        api_open = None

    if api_open is not None and len(api_open) > 0:
        print(f"📋  Open positions (Kalshi): {len(api_open)}")
        print(f"  {'Ticker':<40} {'Side':<5} {'Ctrs':>8} {'Exposure $':>12}")
        print("  " + "-" * 72)
        for p in api_open:
            net = kc.market_position_net_contracts(p)
            side = "YES" if net > 0 else "NO"
            n = abs(net)
            print(
                f"  {p.get('ticker',''):<40} "
                f"{side:<5} "
                f"{n:>8.2f} "
                f"{str(p.get('market_exposure_dollars','')):>12}"
            )
        print()
    else:
        positions: list[dict] = _load_json(_POSITIONS_FILE, [])
        open_pos = [p for p in positions if p.get("status") == "open"]
        label = "bot log" if api_open is not None else "bot log (API failed)"
        print(f"📋  Open positions ({label}): {len(open_pos)}")
        if open_pos:
            print(f"  {'Ticker':<35} {'City':<15} {'Side':<5} {'Ctrs':>4} {'Price':>6} {'Edge':>7}  {'Placed'}")
            print("  " + "-" * 90)
            for p in open_pos:
                print(
                    f"  {p.get('ticker',''):<35} "
                    f"{p.get('city',''):<15} "
                    f"{p.get('recommended_side',''):<5} "
                    f"{p.get('kelly_contracts',0):>4} "
                    f"{p.get('price_cents',0):>5}¢ "
                    f"{(p.get('edge',0) or 0)*100:>+6.1f}¢  "
                    f"{(p.get('placed_at','') or '')[:19]}"
                )
        print()

    # Station scores
    scores: dict = _load_json(_SCORES_FILE, {})
    print(f"📊  Station scores ({len(scores)} cities):")
    if scores:
        print(f"  {'City':<18} {'Score':>6}  {'Bar'}")
        print("  " + "-" * 45)
        for city, score in sorted(scores.items(), key=lambda x: -x[1]):
            bar = "█" * int(score * 10)
            print(f"  {city:<18} {score:>6.3f}  {bar}")
    else:
        print("  (no scores recorded yet)")
    print()


def cmd_simulate(args) -> None:
    """python main.py simulate CITY METRIC THRESHOLD – print edge calculation."""
    city      = args.city
    metric    = args.metric.lower()
    threshold = args.threshold

    if city not in STATION_MAP:
        closest = [k for k in STATION_MAP if args.city.lower() in k.lower()]
        hint = f"  Did you mean: {closest}?" if closest else ""
        print(f"[FAIL] Unknown city {city!r}.{hint}")
        print(f"   Known cities: {', '.join(sorted(STATION_MAP))}")
        sys.exit(1)

    if metric not in ("high", "low"):
        print(f"[FAIL] Metric must be 'high' or 'low', got {metric!r}.")
        sys.exit(1)

    print(f"\n=== SIMULATE: {city} {metric.upper()} {'above' if metric == 'high' else 'below'} {threshold}°F ===\n")

    # Synthesise a fake market dict so calculate_edge can process it.
    direction = "above" if metric == "high" else "below"
    station   = STATION_MAP[city]["station"]

    # Fake market dict with correct title so parse_market recognises it.
    op = ">" if direction == "above" else "<"
    fake_market = {
        "ticker":      f"SIMULATE-{city.upper().replace(' ', '')}-{metric.upper()}-{threshold}",
        "title":       f"Will the {metric} temperature in {city} be {op}{threshold}°F?",
        "strike_type": "greater" if direction == "above" else "less",
        "floor_strike": threshold,
        "yes_ask":    50,
        "yes_bid":    48,
        "no_ask":     52,
        "no_bid":     50,
        "close_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z"),
        "volume":     0,
        "open_interest": 0,
    }

    try:
        balance = kc.get_account_balance()
    except Exception:
        balance = 1000.0  # fallback for simulation

    result = signal_engine.calculate_edge(fake_market, balance)

    if result is None:
        print("⚠️  Could not calculate edge (NWS data may be unavailable or city unparseable).")
        return

    print(f"  City:            {result['city']}")
    print(f"  Station:         {result['station']}")
    print(f"  Metric:          {result['metric'].upper()} {result['direction']} {result['threshold_f']}°F")
    print(f"  NWS forecast:    {result['nws_predicted']}°F  (margin: {result['margin_f']}°F)")
    print(f"  Implied prob:    {result['implied_prob']*100:.1f}%  ({result['implied_prob']*100:.0f}¢)")
    print(f"  Model prob:      {result['model_prob']*100:.1f}%")
    print(f"  Edge:            {result['edge']*100:+.2f}¢")
    print(f"  Signal strength: {result['signal_strength'].upper()}")
    print(f"  Recommended:     {result['recommended_side'].upper()}")
    print(f"  Kelly bet:       ${result['kelly_dollars']:.2f}  ({result['kelly_contracts']} contracts)")
    print(f"  Bankroll used:   ${balance:.2f}\n")


def cmd_trade(args) -> None:
    """python main.py trade – execute all signals above threshold (live orders)."""
    dry_run: bool = getattr(args, "dry_run", False)
    print(f"\n=== PAVLOV-WEATHER-BOT – {'DRY RUN' if dry_run else 'LIVE TRADE'} ===")

    markets = kc.get_weather_markets()
    balance = kc.get_account_balance()
    signals = signal_engine.get_all_signals(markets, balance)
    print(f"Signals found: {len(signals)}  |  Bankroll: ${balance:.2f}\n")

    if not signals:
        print("(no actionable signals — nothing to trade)\n")
        return

    positions = _load_json(_POSITIONS_FILE, [])
    placed = 0

    for sig in signals:
        ticker    = sig["ticker"]
        side      = sig["recommended_side"]
        contracts = sig["kelly_contracts"]
        # Use the ask price for the side we're buying (with a 1¢ buffer).
        if side == "yes":
            raw_price = sig.get("implied_prob", 0.5) * 100
        else:
            raw_price = (1 - sig.get("implied_prob", 0.5)) * 100
        price_cents = max(1, min(99, round(raw_price) + 1))  # +1¢ taker buffer

        dir_label = {
            "above":    f"above {sig['threshold_f']}°F",
            "below":    f"below {sig['threshold_f']}°F",
            "in_range": f"{sig.get('threshold_lo','?')}-{sig.get('threshold_hi','?')}°F",
        }.get(sig['direction'], sig['direction'])

        print(
            f"  >> {sig['city']} {sig['metric'].upper()} {dir_label}\n"
            f"    {side.upper()} {contracts}x @ {price_cents}¢  "
            f"edge={sig['edge']*100:+.1f}¢  [{sig['signal_strength'].upper()}]\n"
            f"    Ticker: {ticker}"
        )

        if dry_run:
            print("    (dry-run: order NOT sent)\n")
            continue

        try:
            result = kc.place_order(ticker, side, contracts, price_cents)
        except Exception as exc:
            print(f"    [FAIL] Order failed: {exc}\n")
            continue

        status = result.get("status", "?")
        filled = result.get("filled_contracts", 0)
        print(f"    [OK]   order_id={result.get('order_id','?')}  status={status}  filled={filled}\n")

        positions.append({
            **sig,
            "order_id":     result.get("order_id", ""),
            "status":       "open",
            "price_cents":  price_cents,
            "placed_at":    datetime.now(timezone.utc).isoformat(),
        })
        placed += 1

    if not dry_run:
        os.makedirs(os.path.dirname(_POSITIONS_FILE), exist_ok=True)
        with open(_POSITIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(positions, fh, indent=2, default=str)
        print(f"Placed {placed} order(s). Positions saved to {_POSITIONS_FILE}\n")



def cmd_cycle_once(_args) -> None:
    """One Kalshi + Polymarket weather cycle; webhook or Discord bot channel."""

    async def _once() -> None:
        await run_cycle()
        await poly_run_cycle()

    asyncio.run(_once())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pavlov-weather-bot",
        description="Kalshi weather prediction trading bot.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run",         help="Start bot + scheduler (production).")
    sub.add_parser("cycle-once",  help="One scan cycle (GHA / webhook mode).")
    sub.add_parser("test",        help="Run one cycle; print signals; no Discord.")
    sub.add_parser("status", help="Print bankroll, positions, and station scores.")

    sim = sub.add_parser(
        "simulate",
        help="Calculate edge for custom parameters without trading.",
    )
    sim.add_argument("city",      help="City name, e.g. 'Chicago'")
    sim.add_argument("metric",    help="'high' or 'low'")
    sim.add_argument("threshold", type=int, help="Temperature threshold in °F")

    trade_p = sub.add_parser(
        "trade",
        help="Execute live orders for all signals above threshold.",
    )
    trade_p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print what would be traded without sending any orders.",
    )

    return parser


_COMMANDS = {
    "run":        cmd_run,
    "cycle-once": cmd_cycle_once,
    "test":       cmd_test,
    "status":   cmd_status,
    "simulate": cmd_simulate,
    "trade":    cmd_trade,
}


if __name__ == "__main__":
    parser = build_parser()
    args   = parser.parse_args()
    _COMMANDS[args.command](args)
