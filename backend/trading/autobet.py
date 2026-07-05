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

    _record_autobet(db, match, market, outcome, winner, edge_res,
                    sizing, mode, bankroll, clob_order_id=clob_order_id,
                    bet_type=bet_type, bet_line=bet_line, sport_label=sport_label)

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
    """Live bankroll = configured bankroll + realised P&L from settled bets."""
    s = get_settings()
    settled = (
        db.table("autobets")
        .select("pnl")
        .not_.is_("resolved_at", "null")
        .execute()
        .data or []
    )
    realised = sum((r.get("pnl") or 0.0) for r in settled)
    return s.polymarket_bankroll + realised


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
    requested_mode = "live" if s.polymarket_live_enabled else "paper"
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
) -> None:
    """Upsert an OPEN autobets row (unique per market+outcome+mode)."""
    shares = round(sizing.stake / edge_res.market_price, 2) if edge_res.market_price > 0 else 0
    record = {
        "match_id": match["id"],
        "market_id": market.market_id,
        "venue": getattr(market, 'venue', 'polymarket'),
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
    }
    # Drop optional columns if migration not applied yet
    optional = ("raw_confidence", "sport", "bet_type", "bet_line")
    try:
        existing = (
            db.table("autobets")
            .select("id")
            .eq("market_id", record["market_id"])
            .eq("outcome_name", record["outcome_name"])
            .eq("mode", mode)
            .eq("status", "open")
            .limit(1)
            .execute()
        )
        if existing.data:
            db.table("autobets").update(record).eq("id", existing.data[0]["id"]).execute()
        else:
            db.table("autobets").insert(record).execute()
    except Exception as exc:
        if "raw_confidence" in str(exc) or "sport" in str(exc) or "bet_type" in str(exc):
            slim = {k: v for k, v in record.items() if k not in optional}
            try:
                existing = (
                    db.table("autobets")
                    .select("id")
                    .eq("market_id", slim["market_id"])
                    .eq("outcome_name", slim["outcome_name"])
                    .eq("mode", mode)
                    .eq("status", "open")
                    .limit(1)
                    .execute()
                )
                if existing.data:
                    db.table("autobets").update(slim).eq("id", existing.data[0]["id"]).execute()
                else:
                    db.table("autobets").insert(slim).execute()
            except Exception as exc2:
                logger.warning(f"Autobet record failed: {exc2}")
        else:
            logger.warning(f"Autobet record failed: {exc}")


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
            "bet_type, bet_line, bet_subject"
        )
        .eq("status", "open")
        .execute()
        .data or []
    )
    if not open_bets:
        return 0

    match_ids = list({b["match_id"] for b in open_bets if b.get("match_id")})
    finished = (
        db.table("matches")
        .select("id, winner, is_final, home_score, away_score")
        .in_("id", match_ids)
        .eq("is_final", True)
        .execute()
        .data or []
    )
    finished_map = {m["id"]: m for m in finished}

    # Fetch team names for open bets (handles duplicate match rows)
    match_meta = {}
    if match_ids:
        meta_rows = (
            db.table("matches")
            .select(
                "id, home_team, away_team, scheduled_at, winner, is_final, "
                "home_score, away_score, match_stats"
            )
            .in_("id", match_ids)
            .execute()
            .data or []
        )
        match_meta = {m["id"]: m for m in meta_rows}

    all_finished = (
        db.table("matches")
        .select(
            "id, home_team, away_team, scheduled_at, winner, is_final, "
            "home_score, away_score, match_stats"
        )
        .eq("is_final", True)
        .execute()
        .data or []
    )

    def _teams_match(a: dict, b: dict) -> bool:
        ah = _canonical(a.get("home_team") or "") or (a.get("home_team") or "").lower()
        aw = _canonical(a.get("away_team") or "") or (a.get("away_team") or "").lower()
        bh = _canonical(b.get("home_team") or "") or (b.get("home_team") or "").lower()
        bw = _canonical(b.get("away_team") or "") or (b.get("away_team") or "").lower()
        return {ah, aw} == {bh, bw}

    def _find_finished(meta: dict) -> dict | None:
        candidates: list[dict] = []
        direct = finished_map.get(meta["id"])
        if direct:
            candidates.append(direct)
        for fm in all_finished:
            if fm["id"] != meta["id"] and _teams_match(meta, fm):
                candidates.append(fm)
        if not candidates:
            return None
        for c in candidates:
            if c.get("winner") and c.get("winner") != "draw":
                return c
        return direct or candidates[0]

    from backend.sports_data.bet_settlement import pick_won_for_autobet

    def _settle_bet(bet: dict, *, allow_regrade: bool) -> bool:
        meta = match_meta.get(bet["match_id"])
        if not meta:
            return False
        match = _find_finished(meta)
        if not match:
            return False

        bet_type = bet.get("bet_type") or "moneyline"
        backed = bet["outcome_name"]
        full_match = {**meta, **match}

        won = pick_won_for_autobet(
            bet_type=bet_type,
            outcome_name=backed,
            bet_line=bet.get("bet_line"),
            bet_subject=bet.get("bet_subject"),
            match=full_match,
            match_stats=full_match.get("match_stats"),
        )
        if won is None:
            return False

        stake = bet.get("stake") or 0.0
        shares = bet.get("shares") or 0.0
        price = bet.get("market_price") or 0.0
        new_status = "won" if won else "lost"
        new_pnl = round(shares * (1 - price), 2) if won else round(-stake, 2)

        if bet.get("status") in ("won", "lost"):
            if not allow_regrade:
                return False
            if bet.get("status") == new_status and bet.get("pnl") == new_pnl:
                return False
            logger.info(
                f"Autobet regrade {bet['id'][:8]}… "
                f"{bet.get('status')} → {new_status} ({backed})"
            )

        db.table("autobets").update({
            "status": new_status,
            "pnl": new_pnl,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", bet["id"]).execute()
        return True

    resolved = 0
    for bet in open_bets:
        if _settle_bet(bet, allow_regrade=False):
            resolved += 1

    settled_bets = (
        db.table("autobets")
        .select(
            "id, match_id, outcome_name, stake, shares, market_price, "
            "bet_type, bet_line, bet_subject, status, pnl"
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
                "id, home_team, away_team, scheduled_at, winner, is_final, "
                "home_score, away_score, match_stats"
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


def update_closing_prices() -> int:
    """
    Fetch all open autobets and simulated bets and update their closing_price and clv to the latest market price.
    Since this runs periodically (e.g. via run_sync), the last update before the bet resolves
    will naturally be the true closing line.
    """
    from backend.trading.polymarket_client import get_active_markets
    from collections import defaultdict
    
    db = get_db()
    
    # Process autobets
    open_bets = (
        db.table("autobets")
        .select("id, sport, market_id, outcome_name, market_price")
        .eq("status", "open")
        .execute()
        .data or []
    )
    
    # Process simulated bets
    sim_bets = (
        db.table("simulated_bets")
        .select("id, sport, market_id, outcome_name, market_price")
        .eq("status", "open")
        .execute()
        .data or []
    )
    
    if not open_bets and not sim_bets:
        return 0
        
    updated_count = 0
    for table_name, bets in [("autobets", open_bets), ("simulated_bets", sim_bets)]:
        if not bets:
            continue
            
        bets_by_sport = defaultdict(list)
        for b in bets:
            bets_by_sport[b.get("sport") or "football"].append(b)
            
        for sport, sport_bets in bets_by_sport.items():
            markets = get_active_markets(sport)
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
                    clv = current_price - original_price
                    
                    try:
                        db.table(table_name).update({
                            "closing_price": current_price,
                            "clv": round(clv, 4)
                        }).eq("id", bet["id"]).execute()
                        updated_count += 1
                    except Exception as exc:
                        logger.debug(f"Failed to update CLV for {table_name} {bet['id']}: {exc}")
                        
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

    paper = not s.polymarket_live_enabled
    learn = learning_summary(paper=paper)

    return {
        "mode": "live" if s.polymarket_live_enabled else "paper",
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
