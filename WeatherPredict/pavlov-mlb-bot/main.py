"""
pavlov-mlb-bot — Discord + MLB Polymarket scheduler.

  python main.py run      Live: Discord + ET-timed cycles (10am / 4pm / midnight / 7am)
  python main.py test     Today's games + signals to stdout (no Discord / no orders)
  python main.py status   Balance, open MLB Poly positions, pitcher & team score leaders
  python main.py games    Today's schedule + pitcher announce status
  python main.py simulate \"Yankees\" \"2025-05-17\"   Full model dump for one game
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

from config import CONFIG
import data_paths as dp
from pipeline import discord_bot, learning_loop, mlb_client, mlb_signal_engine, polymarket_client
from polymarket import mlb_poly_discord

_ET = ZoneInfo("America/New_York")

# Dedupe Discord alerts per ticker+date across 10am/4pm windows
_MLB_ALERT_KEYS: set[str] = set()
_MAX_ALERT_CACHE = 500


def _alert_key(sig: dict) -> str:
    return f"{sig.get('ticker')}|{sig.get('game_date')}"


def _games_and_balance():
    """Sync helpers for CLI (no asyncio)."""
    rows = mlb_client.get_todays_games()
    games = [mlb_signal_engine.schedule_row_to_game(r) for r in rows]
    markets: list = []
    balance = 1000.0
    try:
        if polymarket_client.poly_configured():
            markets = polymarket_client.get_markets(category="sports")
            balance = float(polymarket_client.get_balance())
    except Exception as exc:
        logger.warning("Polymarket unavailable — %s", exc)
    signals = mlb_signal_engine.get_all_signals(games, markets, balance)
    return rows, games, markets, balance, signals


async def run_cycle() -> None:
    """Fetch games, sports markets, signals; post to Discord; resolve + autobet."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, mlb_client.get_todays_games)
    games = [mlb_signal_engine.schedule_row_to_game(r) for r in rows]

    def _fetch_markets_and_balance() -> tuple[list, float]:
        if not polymarket_client.poly_configured():
            return [], 1000.0
        try:
            m = polymarket_client.get_markets(category="sports")
            b = float(polymarket_client.get_balance())
            return m, b
        except Exception as exc:
            logger.warning("run_cycle: Polymarket fetch failed — %s", exc)
            return [], 1000.0

    markets, balance = await loop.run_in_executor(None, _fetch_markets_and_balance)

    signals = await loop.run_in_executor(
        None,
        partial(mlb_signal_engine.get_all_signals, games, markets, balance),
    )
    logger.info(
        "run_cycle: balance=$%.2f games=%d signals=%d markets=%d",
        balance,
        len(games),
        len(signals),
        len(markets),
    )

    channel = discord_bot.get_mlb_poly_channel() or discord_bot.get_channel()
    if channel is not None:
        for sig in signals:
            key = _alert_key(sig)
            if key in _MLB_ALERT_KEYS:
                continue
            await discord_bot.send_mlb_signal(channel, sig)
            _MLB_ALERT_KEYS.add(key)
            while len(_MLB_ALERT_KEYS) > _MAX_ALERT_CACHE:
                _MLB_ALERT_KEYS.pop()
    else:
        logger.warning("run_cycle: no Discord channel — skip MLB signal posts")

    await mlb_poly_discord.maybe_auto_bet_mlb(signals)

    try:
        resolved = await loop.run_in_executor(
            None,
            partial(learning_loop.check_mlb_resolutions, mlb_client, polymarket_client),
        )
        for pos in resolved:
            await mlb_poly_discord.post_mlb_poly_resolution(pos)
    except Exception as exc:
        logger.exception("run_cycle: resolution failed — %s", exc)


async def run_midnight_resolution() -> None:
    """Resolve MLB Polymarket positions after games (midnight ET)."""
    loop = asyncio.get_event_loop()
    try:
        resolved = await loop.run_in_executor(
            None,
            partial(learning_loop.check_mlb_resolutions, mlb_client, polymarket_client),
        )
        for pos in resolved:
            await mlb_poly_discord.post_mlb_poly_resolution(pos)
        logger.info("midnight_resolution: resolved %d positions", len(resolved))
    except Exception as exc:
        logger.exception("midnight_resolution failed — %s", exc)


async def run_morning_summary() -> None:
    """Post MLB Polymarket daily summary (7am ET)."""
    try:
        await mlb_poly_discord.send_mlb_daily_summary_embed()
    except Exception as exc:
        logger.exception("morning_summary failed — %s", exc)


def _next_et_datetime(hour_brooklyn: int, minute: int = 0) -> datetime:
    """Next occurrence of *hour*:*minute* in America/New_York (aware)."""
    now = datetime.now(_ET)
    target = now.replace(hour=hour_brooklyn, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _seconds_until(dt_et: datetime) -> float:
    utc_now = datetime.now(timezone.utc)
    utc_target = dt_et.astimezone(timezone.utc)
    return max(0.5, (utc_target - utc_now).total_seconds())


async def scheduler_loop() -> None:
    """10am ET + 4pm ET cycles; midnight ET resolution; 7am ET summary."""
    for _ in range(60):
        if discord_bot.get_channel() is not None:
            break
        await asyncio.sleep(1)
    else:
        logger.warning("scheduler_loop: Discord main channel not ready after 60s.")

    schedule: list[tuple[str, int, int]] = [
        ("run_cycle", 10, 0),
        ("run_cycle", 16, 0),
        ("midnight", 0, 0),
        ("summary", 7, 0),
    ]

    await run_cycle()

    while True:
        next_when: datetime | None = None
        next_job: str | None = None
        for job, hour, minute in schedule:
            cand = _next_et_datetime(hour, minute)
            if next_when is None or cand < next_when:
                next_when = cand
                next_job = job
        assert next_when is not None and next_job is not None
        await asyncio.sleep(_seconds_until(next_when))
        try:
            if next_job == "run_cycle":
                await run_cycle()
            elif next_job == "midnight":
                await run_midnight_resolution()
            elif next_job == "summary":
                await run_morning_summary()
        except Exception as exc:
            logger.exception("scheduler_loop job %s failed — %s", next_job, exc)


async def main_live() -> None:
    mlb_client.init()
    sr = dp.state_root()
    dp.ensure_state_dirs()
    if sr != dp.app_root():
        logger.info("STATE_DIRECTORY active — persistent logs/data at %s", sr)
    else:
        dp.warn_if_learning_state_ephemeral(logger)
    logger.info(
        "pavlov-mlb-bot live — ET schedule 10:00 / 16:00 cycles | 00:00 resolve | 07:00 summary | "
        "min_edge=%.3f kelly=%.3f",
        CONFIG["MIN_EDGE_THRESHOLD"],
        CONFIG["KELLY_FRACTION"],
    )
    await asyncio.gather(
        discord_bot.run_bot(),
        scheduler_loop(),
    )


def cmd_run(_a: argparse.Namespace) -> None:
    asyncio.run(main_live())


def cmd_test(_a: argparse.Namespace) -> None:
    _, _, markets, balance, signals = _games_and_balance()
    print(f"balance_usd={balance:.2f}  sports_markets={len(markets)}  signals={len(signals)}")
    for i, s in enumerate(signals[:25]):
        print(
            f"[{i + 1}] {s.get('away_team_abbr')}@{s.get('home_team_abbr')} "
            f"edge={s.get('edge'):+.3f} side={s.get('recommended_side')} "
            f"ticker={s.get('ticker')}"
        )
    if len(signals) > 25:
        print(f"... +{len(signals) - 25} more")
    if signals:
        print(json.dumps(signals[0], indent=2, default=str)[:8000])


def cmd_status(_a: argparse.Namespace) -> None:
    mlb_client.init()
    balance = 0.0
    if polymarket_client.poly_configured():
        try:
            balance = float(polymarket_client.get_balance())
        except Exception as exc:
            print(f"balance: error ({exc})", file=sys.stderr)
    print(f"Polymarket balance (USD): {balance:.2f}")

    pos_path = os.path.join(dp.logs_dir(), "positions.json")
    try:
        with open(pos_path, "r", encoding="utf-8") as fh:
            positions = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        positions = []
    open_mlb = [
        p
        for p in positions
        if isinstance(p, dict) and p.get("venue") == "mlb_poly" and p.get("status") == "open"
    ]
    print(f"Open MLB Poly positions: {len(open_mlb)}")
    for p in open_mlb[:15]:
        print(
            f"  {p.get('away_team_abbr')} @ {p.get('home_team_abbr')} | "
            f"{str(p.get('recommended_side', '')).upper()} "
            f"{p.get('kelly_contracts')} @ {p.get('price_cents')}¢ | {p.get('ticker')}"
        )

    pitch_path = os.path.join(dp.logs_dir(), "pitcher_scores.json")
    try:
        with open(pitch_path, "r", encoding="utf-8") as fh:
            pscores = json.load(fh)
        if isinstance(pscores, dict):
            ranked = sorted(
                ((str(k), float(v)) for k, v in pscores.items()),
                key=lambda x: -x[1],
            )[:5]
            print("Top 5 pitcher multipliers:")
            for pid, sc in ranked:
                print(f"  id={pid}  score={sc:.4f}")
        else:
            print("pitcher_scores: (empty or invalid)")
    except FileNotFoundError:
        print("pitcher_scores: (no file yet)")

    team_path = os.path.join(dp.logs_dir(), "team_scores.json")
    try:
        with open(team_path, "r", encoding="utf-8") as fh:
            tscores = json.load(fh)
        if isinstance(tscores, dict):
            mlb_only = [(k, v) for k, v in tscores.items() if str(k).startswith("mlb_")]
            pool = mlb_only if mlb_only else list(tscores.items())
            ranked = sorted(
                ((str(k), float(v)) for k, v in pool),
                key=lambda x: -x[1],
            )[:5]
            label = "MLB team" if mlb_only else "team"
            print(f"Top 5 {label} scores:")
            for tid, sc in ranked:
                print(f"  {tid}  {sc:.4f}")
    except FileNotFoundError:
        print("team_scores: (no file yet)")


def _abbrs_matching_team_query(query: str) -> set[str]:
    q = query.lower().strip()
    out: set[str] = set()
    tz_path = os.path.join(dp.app_root(), "data", "team_timezones.json")
    try:
        with open(tz_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        for abbr, row in meta.items():
            if not isinstance(row, dict):
                continue
            name = (row.get("team_name") or "").lower()
            city = (row.get("city") or "").lower()
            if q in name or q in city or q == str(abbr).lower():
                out.add(str(abbr).upper())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    from pipeline.polymarket_mlb_parser import TEAM_ALIASES

    for alias, abbr in TEAM_ALIASES.items():
        if q in alias:
            out.add(abbr)
    return out


def cmd_simulate(args: argparse.Namespace) -> None:
    mlb_client.init()
    team_q = args.team
    date_str = args.date
    abbrs = _abbrs_matching_team_query(team_q)
    if not abbrs:
        print(f"No team match for {team_q!r}", file=sys.stderr)
        sys.exit(1)

    rows = mlb_client.get_todays_games(date_str)
    pick = None
    for r in rows:
        ha = str((r.get("home") or {}).get("abbr") or "").upper()
        aa = str((r.get("away") or {}).get("abbr") or "").upper()
        fix = {"AZ": "ARI", "TB": "TBR", "KC": "KCR", "SD": "SDP", "SF": "SFG", "WSH": "WSN", "CWS": "CHW"}
        ha = fix.get(ha, ha)
        aa = fix.get(aa, aa)
        if ha in abbrs or aa in abbrs:
            pick = r
            break
    if not pick:
        print(f"No game on {date_str} involving {sorted(abbrs)}", file=sys.stderr)
        sys.exit(1)

    game = mlb_signal_engine.schedule_row_to_game(pick)
    bankroll = 1000.0
    prob = mlb_signal_engine.calculate_win_probability(game, bankroll)
    if not prob:
        print("Model returned None (missing probables or short rest). Raw game row:", file=sys.stderr)
        print(json.dumps(pick, indent=2, default=str))
        sys.exit(2)
    print(json.dumps(prob, indent=2, default=str))


def cmd_games(args: argparse.Namespace) -> None:
    mlb_client.init()
    date_str = args.date or None
    rows = mlb_client.get_todays_games(date_str)
    dlabel = date_str or "today"
    print(f"Games ({dlabel}): {len(rows)}")
    for r in rows:
        hid = (r.get("home_pitcher") or {}) or {}
        aid = (r.get("away_pitcher") or {}) or {}
        ha = (r.get("home") or {}).get("abbr", "?")
        aa = (r.get("away") or {}).get("abbr", "?")
        hn = hid.get("name") or "TBD"
        an = aid.get("name") or "TBD"
        ok_h = bool(hid.get("id"))
        ok_a = bool(aid.get("id"))
        st = "both probables" if ok_h and ok_a else ("home only" if ok_h else "away only" if ok_a else "no probables")
        st_ext = r.get("status") or ""
        tm = r.get("game_time_et") or ""
        print(f"  {aa} @ {ha}  |  {st:16}  |  {tm}  |  {an} vs {hn}  |  sched {st_ext}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pavlov-mlb-bot")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Discord bot + ET MLB scheduler (production)")

    sub.add_parser("test", help="Print today's signals (no Discord, no orders)")

    sub.add_parser("status", help="Balance, open MLB positions, score leaders")

    g_games = sub.add_parser("games", help="List games with pitcher status")
    g_games.add_argument(
        "date",
        nargs="?",
        default=None,
        help="YYYY-MM-DD (default: today in MLB schedule fetch)",
    )

    g_sim = sub.add_parser("simulate", help="Run full model for one team/date")
    g_sim.add_argument("team", help='Team nickname (e.g. "Yankees")')
    g_sim.add_argument("date", help="Game date YYYY-MM-DD")

    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        cmd_run(args)
    elif args.command == "test":
        cmd_test(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "games":
        cmd_games(args)
    elif args.command == "simulate":
        cmd_simulate(args)
    else:
        sys.exit(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
