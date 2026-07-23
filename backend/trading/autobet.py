"""
Autobet orchestrator.

Pipeline (per run):
  1. Pull tradeable Polymarket markets (soccer + relevant search terms).
  2. Pull our upcoming consensus picks (with their feeding pick_count).
  3. For each consensus pick, find the matching Polymarket market + outcome.
  4. Compute the vig-free market probability and our edge.
  5. Size the position through the full risk gate (fractional Kelly + caps).
  6. Record an `autobets` row. In live mode (and only then) also place the order.

Resolution settles open bets from finished match results.

Everything defaults to PAPER mode against REAL market prices, so you build a
verifiable track record before risking a cent.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from backend.config import get_settings
from backend.db import get_db
from backend.trading.edge_model import compute_edge, remove_vig
from backend.trading.market_matcher import (
    match_market_to_db_match,
    map_outcome_to_token,
    is_prop_market,
    match_prop_market_to_db_match,
    map_prop_outcome_to_token,
    outcome_belongs_to_match,
    _canonical,
)
from backend.trading.venue_router import VenueRouter
from backend.trading.autobet_learning import gates_for_price, learning_summary, assess_live_readiness, invalidate_learning_cache
from backend.trading.risk import size_position
from backend.trading.settlement_integrity import (
    EXACT_MATCH_NOT_FINAL,
    EXACT_MATCH_NOT_FOUND,
    SETTLEMENT_PNL_MISMATCH,
    SETTLEMENT_STATUS_MISMATCH,
    SETTLEMENT_VERSION,
    conservative_risk_pnl,
    verify_match_linked_autobet,
)

# Polymarket tag slugs / search terms to scan for relevant markets
MARKET_TAGS = ["soccer", "sports", "mlb", "baseball"]
MARKET_SEARCHES = ["World Cup", "MLB", "baseball"]


async def _markets_for_match(
    client: VenueRouter,
    match: dict,
    markets: list,
    markets_by_id: dict,
) -> list[Any]:
    """All Polymarket markets tied to this DB match (moneyline + draw, etc.)."""
    found: list[Any] = []
    seen: set[str] = set()

    def _add(cand: Any) -> None:
        if cand.market_id not in seen and match_market_to_db_match(cand, [match]):
            seen.add(cand.market_id)
            found.append(cand)

    for cand in markets:
        _add(cand)

    home = match.get("home_team") or ""
    away = match.get("away_team") or ""
    sport = match.get("sport") or "football"
    terms = [
        f"{home} vs {away}",
        f"{home} vs. {away}",
        f"{away} vs {home}",
        f"{away} vs. {home}",
        f"{home} {away}",
        f"{home} vs {away} draw",
        f"{home} vs {away} tie",
    ]
    if sport == "mlb":
        terms.extend([
            home, away,
            f"{home} MLB", f"{away} MLB", f"{home} baseball",
            f"{away} vs. {home}: O/U",
        ])
    else:
        terms.extend([f"{home} World Cup", home])
    search_limit = 80 if sport == "mlb" else 50
    for term in terms:
        if not term.strip():
            continue
        for m in await client.fetch_markets(search=term, limit=search_limit):
            markets_by_id.setdefault(m.market_id, m)
            _add(m)

    return found


async def _find_prop_market(
    client: VenueRouter,
    match: dict,
    bet_type: str,
    predicted_winner: str,
    bet_line: str | None,
    markets: list,
    markets_by_id: dict,
) -> Any | None:
    """Find a Polymarket prop market matching this consensus signal."""
    async def _scan(candidates: list) -> Any | None:
        for m in candidates:
            if not is_prop_market(m):
                continue
            if not match_prop_market_to_db_match(
                m, match, bet_type=bet_type, bet_line=bet_line,
            ):
                continue
            if map_prop_outcome_to_token(
                m,
                bet_type=bet_type,
                predicted_winner=predicted_winner,
                bet_line=bet_line,
            ):
                return m
        return None

    match_markets = await _markets_for_match(client, match, markets, markets_by_id)
    found = await _scan(match_markets)
    if found:
        return found
    return await _scan(markets)


async def _find_market_for_winner(
    client: VenueRouter,
    match: dict,
    winner: str,
    markets: list,
    markets_by_id: dict,
) -> Any | None:
    """Pick the Polymarket market whose outcome token maps to this consensus winner."""

    def _pick_moneyline(candidates: list) -> Any | None:
        for m in candidates:
            if is_prop_market(m):
                continue
            if map_outcome_to_token(m, winner, match):
                return m
        return None

    match_markets = await _markets_for_match(client, match, markets, markets_by_id)
    found = _pick_moneyline(match_markets)
    if found:
        return found

    # MLB moneylines often surface only via single-team search (not "A vs B").
    if (match.get("sport") or "").lower() == "mlb":
        for term in (match.get("home_team"), match.get("away_team")):
            if not term:
                continue
            for m in await client.fetch_markets(search=term, limit=80):
                markets_by_id.setdefault(m.market_id, m)
                if not match_market_to_db_match(m, [match]):
                    continue
                if is_prop_market(m):
                    continue
                if map_outcome_to_token(m, winner, match):
                    return m
    return None


async def _evaluate_autobet_candidate(
    *,
    client: VenueRouter,
    match: dict,
    winner: str,
    raw_confidence: float,
    picker_count: int,
    market,
    s,
    mode: str,
    bankroll: float,
    total_exposure: float,
    event_exposure: dict,
    db,
    bet_type: str = "moneyline",
    bet_line: str | None = None,
) -> tuple[str, dict | None]:
    """
    Returns ('placed'|'rejected'|'skip', signal_dict|None).
    """
    if bet_type in ("total_goals", "btts"):
        outcome = map_prop_outcome_to_token(
            market,
            bet_type=bet_type,
            predicted_winner=winner,
            bet_line=bet_line,
        )
    else:
        outcome = map_outcome_to_token(market, winner, match)
    if not outcome:
        return "skip", None

    vig_free = remove_vig([o.mid_price for o in market.outcomes])
    idx = market.outcomes.index(outcome)
    market_prob = vig_free[idx] if idx < len(vig_free) else outcome.mid_price
    market_price = outcome.best_ask or outcome.mid_price

    # Hard go-live gating: sports longshots are unproven under the new CLV system and need fresh paper trading
    base_sport = match.get("sport") or "football"
    sport_label = base_sport
    
    paper = mode == "paper"
    min_liq = s.polymarket_paper_min_liquidity if paper else s.polymarket_min_liquidity
    edge_res = compute_edge(
        raw_confidence=raw_confidence,
        market_price=market_price,
        picker_count=picker_count,
        fee_bps=s.polymarket_fee_bps,
        sport=base_sport,
        paper_mode=paper,
        min_history_override=s.polymarket_paper_min_history if paper else None,
        max_model_weight_override=s.polymarket_paper_max_model_weight if paper else None,
    )
    
    if base_sport in ("football", "mlb"):
        if edge_res.model_prob >= 0.50:
            sport_label = f"{base_sport}_fav"
        elif edge_res.model_prob >= 0.15:
            sport_label = f"{base_sport}_dog"
        else:
            sport_label = f"{base_sport}_longshot"
            
    if sport_label in ("football_longshot", "mlb_longshot"):
        mode = "paper"
        paper = True

    _, depth = await client.get_book_depth(venue=market.venue, token_id=outcome.token_id, market_id=market.market_id, side="sell")
    if depth <= 0:
        depth = market.liquidity

    tier_gates = gates_for_price(
        market_price,
        paper=paper,
        raw_confidence=raw_confidence,
        sport=sport_label,
    )
    # Skip if we already track this pick (prevents re-run stake inflation)
    if (
        db.table("autobets")
        .select("id")
        .eq("match_id", match["id"])
        .eq("outcome_name", winner)
        .eq("mode", mode)
        .eq("status", "open")
        .limit(1)
        .execute()
        .data
    ):
        return "skip", None

    sizing = size_position(
        model_prob=edge_res.model_prob,
        market_price=market_price,
        edge=edge_res.edge,
        bankroll=bankroll,
        current_total_exposure=total_exposure,
        current_event_exposure=event_exposure.get(match["id"], 0.0),
        book_depth_usdc=depth,
        min_edge=tier_gates.min_edge,
        min_model_prob=tier_gates.min_model_prob,
        min_liquidity=min_liq,
        paper=paper,
    )

    if not sizing.approved:
        logger.info(
            f"Autobet skip {winner} ({match.get('home_team')} v {match.get('away_team')}): "
            f"{sizing.reject_reason} | tier={tier_gates.tier} "
            f"raw={raw_confidence:.2f} mkt={market_price:.2f} "
            f"model={edge_res.model_prob:.2f} w={edge_res.model_weight:.2f}"
        )
        return "rejected", None

    clob_order_id = None
    if mode == "live":
        if market.venue == 'kalshi':
            logger.warning('Kalshi live orders not yet implemented')
            return 'rejected', None
        else:
            result = client.poly.place_order(outcome.token_id, market_price, sizing.stake)
        if not result["ok"]:
            logger.warning(f"Autobet live order failed for {winner}: {result['error']}")
            return "rejected", None
        clob_order_id = result["order_id"]

    _recorded = _record_autobet(db, match, market, outcome, winner, edge_res,
                    sizing, mode, bankroll, clob_order_id=clob_order_id,
                    bet_type=bet_type, bet_line=bet_line, sport_label=sport_label)
    if not _recorded:
        return "rejected", None

    pick_label = winner
    if bet_type == "total_goals" and bet_line:
        pick_label = f"{winner} {bet_line}"
    elif bet_type == "btts":
        pick_label = f"BTTS {winner}"

    return "placed", {
        "match": f"{match.get('home_team')} vs {match.get('away_team')}",
        "pick": pick_label,
        "bet_type": bet_type,
        "edge": edge_res.edge,
        "model_prob": edge_res.model_prob,
        "market_price": market_price,
        "stake": sizing.stake,
    }


def normalize_open_autobet_stakes() -> int:
    """Cap inflated open paper stakes to the current paper position limit."""
    db = get_db()
    s = get_settings()
    bankroll = _current_bankroll(db)
    cap = round(bankroll * s.polymarket_paper_max_position_pct, 2)
    open_bets = (
        db.table("autobets")
        .select("id, stake, market_price, shares")
        .eq("status", "open")
        .eq("mode", "paper")
        .execute()
        .data or []
    )
    updated = 0
    for bet in open_bets:
        stake = bet.get("stake") or 0.0
        if stake <= cap:
            continue
        price = bet.get("market_price") or 0.5
        shares = round(cap / price, 2) if price > 0 else bet.get("shares")
        db.table("autobets").update({
            "stake": cap,
            "shares": shares,
        }).eq("id", bet["id"]).execute()
        updated += 1
    if updated:
        logger.info(f"Normalized {updated} open autobet stakes to ${cap:.2f} cap")
    return updated


def _current_bankroll(db) -> float:
    """Configured bankroll plus verified or conservative realized P&L."""
    s = get_settings()
    try:
        settled = (
            db.table("autobets")
            .select(
                "id, match_id, sport, outcome_name, status, pnl, stake, shares, "
                "market_price, bet_type, bet_line, bet_subject, resolved_at, "
                "settlement_version, settlement_match_id, settlement_corrected_at, "
                "matches:matches!autobets_match_id_fkey("
                "id, sport, external_id, home_team, away_team, scheduled_at, "
                "finished_at, winner, is_final, home_score, away_score, match_stats)"
            )
            .not_.is_("resolved_at", "null")
            .execute()
            .data
            or []
        )
        realised = 0.0
        conservative_count = 0
        for row in settled:
            match = row.get("matches")
            if isinstance(match, list):
                match = match[0] if len(match) == 1 else None
            if not isinstance(match, dict):
                conservative_count += 1
                realised += min(
                    float(row.get("pnl") or 0.0),
                    -abs(float(row.get("stake") or 0.0)),
                )
                continue
            check = verify_match_linked_autobet(row, match)
            if not check.valid:
                conservative_count += 1
            realised += conservative_risk_pnl(row, check)
        if conservative_count:
            logger.warning(
                "Bankroll integrity: {}/{} settled rows treated conservatively",
                conservative_count,
                len(settled),
            )
        return s.polymarket_bankroll + realised
    except Exception as exc:
        logger.error(
            "Bankroll integrity verification failed closed; using configured bankroll: {}",
            exc,
        )
        return s.polymarket_bankroll


def _open_exposures(db) -> tuple[float, dict[str, float]]:
    """Return (total open stake, {match_id: open stake on that match})."""
    open_bets = (
        db.table("autobets")
        .select("match_id, stake")
        .eq("status", "open")
        .execute()
        .data or []
    )
    total = 0.0
    by_event: dict[str, float] = defaultdict(float)
    for b in open_bets:
        stake = b.get("stake") or 0.0
        total += stake
        if b.get("match_id"):
            by_event[b["match_id"]] += stake
    return total, by_event


async def run_autobet() -> dict[str, Any]:
    """Scan markets, evaluate edges, and place (paper/live) bets. Returns summary."""
    s = get_settings()
    db = get_db()
    client = VenueRouter()
    from backend.trading.live_toggle import is_live_mode
    requested_mode = "live" if is_live_mode(s, db) else "paper"
    mode = requested_mode
    live_blocked = False

    if requested_mode == "live":
        # 1. Check Guardian Halt
        import json
        import os
        halt_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".guardian_halt.json")
        if os.path.exists(halt_file):
            try:
                with open(halt_file, "r") as f:
                    state = json.load(f)
                    if state.get("halted"):
                        logger.warning(f"Live autobet blocked by Guardian: {state.get('reasons')}")
                        mode = "paper"
                        live_blocked = True
            except Exception as exc:
                logger.error(f"Failed to read guardian halt file: {exc}")
                
        # 2. Check Live Readiness (only if not already halted)
        if not live_blocked:
            readiness = assess_live_readiness(db)
            if not readiness["live_ready"]:
                logger.warning(f"Live autobet blocked: {readiness['message']}")
                mode = "paper"
                live_blocked = True

    # 1. Pull markets (dedupe by market_id)
    markets_by_id: dict[str, Any] = {}
    for tag in MARKET_TAGS:
        for m in await client.fetch_markets(tag_slug=tag):
            markets_by_id[m.market_id] = m
    for term in MARKET_SEARCHES:
        for m in await client.fetch_markets(search=term):
            markets_by_id.setdefault(m.market_id, m)
    markets = list(markets_by_id.values())

    if not markets:
        logger.info("Autobet: no tradeable markets found")
        return {"mode": mode, "evaluated": 0, "placed": 0, "rejected": 0}

    invalidate_learning_cache()

    # 2. Upcoming consensus picks
    paper = mode == "paper"
    consensus = (
        db.table("consensus_picks")
        .select(
            "match_id, predicted_winner, confidence, pick_count, "
            "bet_type, bet_line, consensus_key, "
            "draw_probability, home_probability, away_probability, "
            "matches(id, home_team, away_team, scheduled_at, is_final, sport)"
        )
        .execute()
        .data or []
    )
    min_moneyline_conf = s.polymarket_paper_min_consensus_confidence if paper else 0.50
    min_prop_conf = s.polymarket_paper_min_prop_confidence if paper else 0.50
    consensus = [
        c for c in consensus
        if c.get("matches") and not c["matches"].get("is_final")
        and (c.get("confidence") or 0)
        >= (
            min_prop_conf
            if (c.get("bet_type") or "moneyline") in ("total_goals", "btts", "draw")
            else min_moneyline_conf
        )
    ]
    # Drop matches whose scheduled start is already in the past (stale is_final=false rows).
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    fresh: list[dict] = []
    for c in consensus:
        match = c.get("matches") or {}
        sched_raw = match.get("scheduled_at")
        if not sched_raw:
            fresh.append(c)
            continue
        try:
            sched = datetime.fromisoformat(str(sched_raw).replace("Z", "+00:00"))
        except Exception:
            fresh.append(c)
            continue
        # Allow a small grace window after first pitch for late shadow fills.
        if sched >= now - timedelta(hours=1):
            fresh.append(c)
        else:
            logger.debug(
                f"Autobet: skip stale match {match.get('away_team')} @ {match.get('home_team')} "
                f"(scheduled {sched_raw})"
            )
    consensus = fresh
    if not consensus:
        logger.info("Autobet: no upcoming consensus picks")
        return {"mode": mode, "evaluated": 0, "placed": 0, "rejected": 0}

    # One evaluation per match + pick (consensus table may have duplicate rows)
    seen_keys: set[tuple] = set()
    deduped: list[dict] = []
    for c in consensus:
        key = (
            c.get("match_id"),
            c.get("predicted_winner"),
            c.get("bet_type") or "moneyline",
            c.get("bet_line"),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(c)
    consensus = deduped

    # Seed MLB markets by team name — Polymarket rarely returns game ML via tag bulk fetch.
    for c in consensus:
        match = c.get("matches") or {}
        if match.get("sport") != "mlb":
            continue
        for term in (match.get("home_team"), match.get("away_team")):
            if not term:
                continue
            for m in await client.fetch_markets(search=term, limit=80):
                markets_by_id.setdefault(m.market_id, m)
    markets = list(markets_by_id.values())

    db_matches = [c["matches"] for c in consensus]

    # 3. Shared state: bankroll, exposures
    bankroll = _current_bankroll(db)
    total_exposure, event_exposure = _open_exposures(db)

    evaluated = placed = rejected = 0
    placed_signals: list[dict] = []

    for c in consensus:
        match = c["matches"]
        bet_type = c.get("bet_type") or "moneyline"
        bet_line = c.get("bet_line")
        conf = c.get("confidence") or 0.0
        winner = c.get("predicted_winner")

        candidates: list[tuple[str, float, str, str | None]] = []

        if bet_type in ("total_goals", "btts"):
            if winner:
                candidates.append((winner, conf, bet_type, bet_line))
        elif bet_type == "draw":
            if winner and match.get("sport") != "mlb":
                candidates.append((winner, conf, "draw", None))
        else:
            # Moneyline: consensus winner + optional draw side-bet
            if (
                winner
                and winner not in ("over", "under", "yes", "no")
                and outcome_belongs_to_match(winner, match)
            ):
                candidates.append((winner, conf, "moneyline", None))
            elif winner and not outcome_belongs_to_match(winner, match):
                logger.warning(
                    f"Autobet: skip consensus {winner} — not a team in "
                    f"{match.get('home_team')} vs {match.get('away_team')}"
                )
            draw_prob = c.get("draw_probability") or 0.0
            if draw_prob >= 0.20 and c.get("predicted_winner") != "draw":
                candidates.append(("draw", draw_prob, "draw", None))

        for winner, pick_conf, pick_bet_type, pick_bet_line in candidates:
            if pick_bet_type in ("total_goals", "btts"):
                market = await _find_prop_market(
                    client, match, pick_bet_type, winner, pick_bet_line,
                    markets, markets_by_id,
                )
            else:
                market = await _find_market_for_winner(
                    client, match, winner, markets, markets_by_id
                )
            if not market:
                logger.info(
                    f"Autobet: no Polymarket market for {pick_bet_type}/{winner} "
                    f"({match.get('home_team')} vs {match.get('away_team')})"
                )
                continue

            evaluated += 1
            status, signal = await _evaluate_autobet_candidate(
                client=client,
                match=match,
                winner=winner,
                raw_confidence=pick_conf,
                picker_count=c.get("pick_count") or 0,
                market=market,
                s=s,
                mode=mode,
                bankroll=bankroll,
                total_exposure=total_exposure,
                event_exposure=event_exposure,
                db=db,
                bet_type=pick_bet_type,
                bet_line=pick_bet_line,
            )
            if status == "placed" and signal:
                total_exposure += signal["stake"]
                event_exposure[match["id"]] = event_exposure.get(match["id"], 0.0) + signal["stake"]
                placed += 1
                placed_signals.append(signal)
            elif status == "rejected":
                rejected += 1

    logger.info(
        f"Autobet [{mode}]: evaluated={evaluated} placed={placed} rejected={rejected} "
        f"bankroll=${bankroll:.2f}"
    )
    return {
        "mode": mode,
        "requested_mode": requested_mode,
        "live_blocked": live_blocked,
        "evaluated": evaluated,
        "placed": placed,
        "rejected": rejected,
        "bankroll": round(bankroll, 2),
        "signals": placed_signals,
    }


def _record_autobet(
    db, match: dict, market, outcome, winner: str,
    edge_res, sizing, mode: str, bankroll: float,
    *, clob_order_id: str | None = None,
    bet_type: str = "moneyline",
    bet_line: str | None = None,
    sport_label: str = "football",
) -> bool:
    """Upsert an OPEN autobets row (unique per market+outcome+mode).

    Returns True when the row was persisted. Unknown/optional schema columns
    (``venue``, ``metadata``, etc.) are retried without so paper trades are
    never silently dropped when a migration hasn't landed yet.
    """
    shares = round(sizing.stake / edge_res.market_price, 2) if edge_res.market_price > 0 else 0
    venue = getattr(market, "venue", None) or "polymarket"
    event_date = None
    try:
        from zoneinfo import ZoneInfo

        scheduled = datetime.fromisoformat(
            str(match.get("scheduled_at")).replace("Z", "+00:00")
        )
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        event_date = scheduled.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except (TypeError, ValueError):
        pass
    sport_lower = (sport_label or "").lower()
    strategy = (
        "legacy_consensus_mlb"
        if "mlb" in sport_lower or "baseball" in sport_lower
        else "legacy_consensus_football"
        if "football" in sport_lower or "soccer" in sport_lower
        else "legacy_other"
    )
    record = {
        "match_id": match["id"],
        "event_date": event_date,
        "strategy": strategy,
        "market_id": market.market_id,
        "venue": venue,
        "market_slug": market.slug,
        "question": market.question[:500],
        "outcome_name": winner,
        "token_id": outcome.token_id,
        "mode": mode,
        "model_prob": edge_res.model_prob,
        "market_prob": edge_res.market_prob,
        "market_price": edge_res.market_price,
        "edge": edge_res.edge,
        "raw_confidence": edge_res.raw_confidence,
        "sport": sport_label,
        "kelly_fraction": sizing.kelly_fraction,
        "stake": sizing.stake,
        "bankroll_at_time": round(bankroll, 2),
        "shares": shares,
        "status": "open",
        "clob_order_id": clob_order_id,
        "bet_type": bet_type,
        "bet_line": bet_line,
        "metadata": {"venue": venue},
    }
    # Drop optional columns if migration not applied yet
    optional = (
        "raw_confidence",
        "sport",
        "bet_type",
        "bet_line",
        "venue",
        "metadata",
        "event_date",
        "strategy",
    )

    def _upsert(payload: dict) -> None:
        existing = (
            db.table("autobets")
            .select("id")
            .eq("market_id", payload["market_id"])
            .eq("outcome_name", payload["outcome_name"])
            .eq("mode", mode)
            .eq("status", "open")
            .limit(1)
            .execute()
        )
        if existing.data:
            db.table("autobets").update(payload).eq("id", existing.data[0]["id"]).execute()
        else:
            db.table("autobets").insert(payload).execute()

    try:
        _upsert(record)
        return True
    except Exception as exc:
        msg = str(exc)
        drop = [col for col in optional if col in msg]
        if not drop and ("PGRST204" in msg or "schema cache" in msg):
            drop = list(optional)
        if drop:
            slim = {k: v for k, v in record.items() if k not in drop}
            try:
                _upsert(slim)
                logger.warning(
                    f"Autobet recorded without columns {drop} "
                    f"(apply supabase migration for full schema)"
                )
                return True
            except Exception as exc2:
                logger.warning(f"Autobet record failed: {exc2}")
                return False
        logger.warning(f"Autobet record failed: {exc}")
        return False


def resolve_autobets() -> int:
    """
    Settle open autobets whose match is finished.
      win  → pnl = shares × (1 − price)   (a $1 contract bought at `price`)
      loss → pnl = −stake
    Returns the number of bets resolved.
    """
    db = get_db()
    open_bets = (
        db.table("autobets")
        .select(
            "id, match_id, outcome_name, stake, shares, market_price, "
            "bet_type, bet_line, bet_subject, sport, status, pnl, resolved_at, "
            "settlement_version, settlement_match_id, settlement_corrected_at"
        )
        .eq("status", "open")
        .execute()
        .data or []
    )
    match_ids = list({b["match_id"] for b in open_bets if b.get("match_id")})
    # Fetch only the exact linked match rows. Never search by team names.
    match_meta = {}
    if match_ids:
        meta_rows = (
            db.table("matches")
            .select(
                "id, sport, external_id, home_team, away_team, scheduled_at, "
                "finished_at, winner, is_final, home_score, away_score, match_stats"
            )
            .in_("id", match_ids)
            .execute()
            .data or []
        )
        match_meta = {m["id"]: m for m in meta_rows}

    def _settle_bet(bet: dict, *, allow_regrade: bool) -> bool:
        match = match_meta.get(bet.get("match_id"))
        if not match:
            logger.warning("{} autobet={}", EXACT_MATCH_NOT_FOUND, bet.get("id"))
            return False
        if match.get("is_final") is not True:
            logger.info("{} autobet={}", EXACT_MATCH_NOT_FINAL, bet.get("id"))
            return False

        now = datetime.now(timezone.utc)
        check = verify_match_linked_autobet(bet, match, now=now)
        if check.valid:
            return False
        if check.reason not in (SETTLEMENT_STATUS_MISMATCH, SETTLEMENT_PNL_MISMATCH):
            logger.warning("{} autobet={}", check.reason, bet.get("id"))
            return False
        if check.expected_status is None or check.expected_pnl is None:
            logger.warning("SETTLEMENT_DATA_INCOMPLETE autobet={}", bet.get("id"))
            return False

        new_status = check.expected_status
        new_pnl = check.expected_pnl
        backed = bet.get("outcome_name")
        already_settled = bet.get("status") in ("won", "lost")
        if bet.get("status") in ("won", "lost"):
            if not allow_regrade:
                return False
            if bet.get("status") == new_status and bet.get("pnl") == new_pnl:
                return False
            logger.info(
                f"Autobet regrade {bet['id'][:8]}… "
                f"{bet.get('status')} → {new_status} ({backed})"
            )

        update = {
            "status": new_status,
            "pnl": new_pnl,
            "settlement_version": SETTLEMENT_VERSION,
            "settlement_match_id": bet["match_id"],
        }
        if already_settled:
            update["settlement_corrected_at"] = now.isoformat()
        else:
            update["resolved_at"] = now.isoformat()
        db.table("autobets").update(update).eq("id", bet["id"]).execute()
        return True

    resolved = 0
    for bet in open_bets:
        if _settle_bet(bet, allow_regrade=False):
            resolved += 1

    settled_bets = (
        db.table("autobets")
        .select(
            "id, match_id, outcome_name, stake, shares, market_price, "
            "bet_type, bet_line, bet_subject, sport, status, pnl, resolved_at, "
            "settlement_version, settlement_match_id, settlement_corrected_at"
        )
        .in_("status", ["won", "lost"])
        .not_.is_("match_id", "null")
        .execute()
        .data or []
    )
    # Extend match_meta for any settled bet not already loaded
    extra_ids = {b["match_id"] for b in settled_bets if b.get("match_id")} - set(match_meta)
    if extra_ids:
        extra_rows = (
            db.table("matches")
            .select(
                "id, sport, external_id, home_team, away_team, scheduled_at, "
                "finished_at, winner, is_final, home_score, away_score, match_stats"
            )
            .in_("id", list(extra_ids))
            .execute()
            .data or []
        )
        for m in extra_rows:
            match_meta[m["id"]] = m
    for bet in settled_bets:
        if _settle_bet(bet, allow_regrade=True):
            resolved += 1

    if resolved:
        logger.info(f"Autobet: resolved {resolved} bets")
    return resolved


async def update_closing_prices() -> int:
    """
    Fetch all open autobets and simulated bets and update their closing_price and clv to the latest market price.
    Since this runs periodically (e.g. via run_sync), the last update before the bet resolves
    will naturally be the true closing line.
    """
    from backend.trading.venue_router import VenueRouter
    from collections import defaultdict
    from loguru import logger
    
    db = get_db()
    
    # Process autobets
    open_bets = (
        db.table("autobets")
        .select("id, sport, market_id, outcome_name, market_price")
        .eq("status", "open")
        .execute()
        .data or []
    )
    
    # simulated_bets schema has no sport/status/market_id/outcome_name —
    # never let a bad select block autobet CLV or settlement.
    sim_bets: list[dict] = []
    try:
        sim_probe = (
            db.table("simulated_bets")
            .select("id, closing_price, clv")
            .is_("resolved_at", "null")
            .limit(1)
            .execute()
        )
        if sim_probe.data:
            logger.info(
                "Skipping simulated_bets closing-price update "
                "(table has no market_id/sport columns)"
            )
    except Exception as exc:
        logger.warning(f"simulated_bets CLV query skipped: {exc}")
    
    if not open_bets and not sim_bets:
        return 0
        
    updated_count = 0
    for table_name, bets in [("autobets", open_bets), ("simulated_bets", sim_bets)]:
        if not bets:
            continue
            
        bets_by_sport = defaultdict(list)
        for b in bets:
            bets_by_sport[b.get("sport") or "football"].append(b)
            
        import asyncio
        router = VenueRouter()
        
        for sport, sport_bets in bets_by_sport.items():
            markets = await router.fetch_markets(search=sport, limit=100)
            market_map = {m.market_id: m for m in markets}
            
            for bet in sport_bets:
                m = market_map.get(bet["market_id"])
                if not m:
                    continue
                
                current_price = None
                for out in m.outcomes:
                    if out.name.lower() == str(bet.get("outcome_name", "")).lower():
                        current_price = out.price
                        break
                        
                if current_price is not None:
                    original_price = float(bet.get("market_price") or 0.0)
                    from backend.trading.clv_tracker import calculate_clv
                    clv = calculate_clv(original_price, current_price)
                    
                    try:
                        db.table(table_name).update({
                            "closing_price": current_price,
                            "clv": round(clv, 4)
                        }).eq("id", bet["id"]).execute()
                        updated_count += 1
                    except Exception as exc:
                        logger.warning(f"CLV update failed for {table_name} {bet.get('id')}: {exc}")
    return updated_count


def get_autobet_summary() -> dict[str, Any]:
    """Performance summary for the dashboard."""
    s = get_settings()
    db = get_db()
    bets = (
        db.table("autobets")
        .select("stake, pnl, status, edge, mode")
        .neq("status", "rejected")
        .execute()
        .data or []
    )
    settled = [b for b in bets if b.get("status") in ("won", "lost")]
    wins = sum(1 for b in settled if b["status"] == "won")
    total_staked = sum(b.get("stake") or 0 for b in settled)
    total_pnl = sum(b.get("pnl") or 0 for b in settled)
    open_count = sum(1 for b in bets if b.get("status") == "open")
    open_stake = sum(b.get("stake") or 0 for b in bets if b.get("status") == "open")

    from backend.trading.live_toggle import is_live_mode
    live_now = is_live_mode(s, db)
    paper = not live_now
    learn = learning_summary(paper=paper)

    return {
        "mode": "live" if live_now else "paper",
        "starting_bankroll": s.polymarket_bankroll,
        "bankroll": round(s.polymarket_bankroll + total_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(total_pnl / total_staked * 100, 2) if total_staked else 0.0,
        "settled_bets": len(settled),
        "win_rate": round(wins / len(settled), 4) if settled else 0.0,
        "open_bets": open_count,
        "open_exposure": round(open_stake, 2),
        "total_staked": round(total_staked, 2),
        "learning": learn,
        "live_readiness": learn.get("live_readiness"),
    }
