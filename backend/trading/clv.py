"""
Closing Line Value (CLV) snapshotting.

For every pending pick that's linked to a match we can find on Polymarket, we
record the market's vig-free implied probability that the pick is correct, into
`picks.market_prob_at_pick`.

This serves two purposes:
  1. Feeds CLV-aware Elo (backend/ml/elo_ranker.py): pickers are scored against
     the market line, so beating long-shots is rewarded more than agreeing with
     heavy favourites.
  2. Lets us measure each picker's average CLV — the single best predictor of
     long-run betting skill.

We only write the snapshot ONCE per pick (when the column is still null), so it
genuinely reflects the line at the time we first observed the pick, not the
closing line after it drifts.
"""
from __future__ import annotations

from loguru import logger

from backend.db import get_db
from backend.trading.edge_model import remove_vig
from backend.trading.market_matcher import match_market_to_db_match, map_outcome_to_token
from backend.trading.polymarket_client import PolymarketClient

MARKET_TAGS = ["soccer", "sports"]
MARKET_SEARCHES = ["World Cup", "MLB"]


async def snapshot_pick_market_probs() -> int:
    """
    Snapshot market implied probability onto pending picks lacking one.
    Returns the number of picks updated.
    """
    db = get_db()

    pending = (
        db.table("picks")
        .select("id, predicted_winner, match_id, bet_type, "
                "matches(id, home_team, away_team, scheduled_at, is_final)")
        .eq("outcome", "pending")
        .is_("market_prob_at_pick", "null")
        .not_.is_("match_id", "null")
        .execute()
        .data or []
    )
    # Only moneyline / draw picks map cleanly to winner markets
    pending = [
        p for p in pending
        if p.get("matches")
        and not p["matches"].get("is_final")
        and (p.get("bet_type") in ("moneyline", "draw", None))
        and p.get("predicted_winner") not in ("over", "under", "yes", "no")
    ]
    if not pending:
        return 0

    client = PolymarketClient()
    markets_by_id = {}
    for tag in MARKET_TAGS:
        for m in await client.fetch_markets(tag_slug=tag):
            markets_by_id[m.market_id] = m
    for term in MARKET_SEARCHES:
        for m in await client.fetch_markets(search=term):
            markets_by_id.setdefault(m.market_id, m)
    markets = list(markets_by_id.values())
    if not markets:
        return 0

    updated = 0
    for pick in pending:
        match = pick["matches"]
        winner = pick["predicted_winner"]

        market = None
        for cand in markets:
            if match_market_to_db_match(cand, [match]):
                market = cand
                break
        if not market:
            continue

        outcome = map_outcome_to_token(market, winner, match)
        if not outcome:
            continue

        vig_free = remove_vig([o.mid_price for o in market.outcomes])
        idx = market.outcomes.index(outcome)
        market_prob = vig_free[idx] if idx < len(vig_free) else outcome.mid_price

        if not (0.0 < market_prob < 1.0):
            continue

        try:
            db.table("picks").update(
                {"market_prob_at_pick": round(market_prob, 4)}
            ).eq("id", pick["id"]).execute()
            updated += 1
        except Exception as exc:
            logger.debug(f"CLV snapshot skipped for pick {pick['id']}: {exc}")

    logger.info(f"CLV: snapshotted market prob on {updated} picks")
    return updated


def compute_average_clv() -> int:
    """
    For each influencer, compute average CLV per sport across resolved picks
    with a market_prob_at_pick snapshot. Stores headline avg_clv plus
    avg_clv_by_sport so MLB and football CLV do not cross-contaminate.
    """
    db = get_db()
    rows = (
        db.table("picks")
        .select("influencer_id, outcome, market_prob_at_pick, matches(sport)")
        .in_("outcome", ["correct", "incorrect"])
        .not_.is_("market_prob_at_pick", "null")
        .execute()
        .data or []
    )
    if not rows:
        return 0

    from collections import defaultdict
    by_inf_sport: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        mp = r.get("market_prob_at_pick")
        if mp is None:
            continue
        sport = ((r.get("matches") or {}).get("sport")) or "football"
        actual = 1.0 if r["outcome"] == "correct" else 0.0
        by_inf_sport[r["influencer_id"]][str(sport).lower()].append(actual - mp)

    updated = 0
    for iid, sport_map in by_inf_sport.items():
        avg_by_sport: dict[str, float] = {}
        all_clvs: list[float] = []
        for sport, clvs in sport_map.items():
            if not clvs:
                continue
            avg_by_sport[sport] = round(sum(clvs) / len(clvs), 4)
            all_clvs.extend(clvs)
        if not all_clvs:
            continue
        payload: dict = {
            "avg_clv": round(sum(all_clvs) / len(all_clvs), 4),
            "avg_clv_by_sport": avg_by_sport,
        }
        try:
            db.table("influencers").update(payload).eq("id", iid).execute()
            updated += 1
        except Exception:
            pass

    logger.info(f"CLV: updated avg_clv for {updated} influencers (per-sport)")
    return updated
