"""
Autobet learning — feedback loops for paper/live placement.

Tracks ROI by price tier, sport, and upset-trap profile (high consensus vs low
market). Settled bets tighten/relax gates and gate live-mode promotion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from loguru import logger

from backend.config import get_settings
from backend.db import get_db

PRICE_TIERS: list[tuple[float, float, str]] = [
    (0.0, 0.15, "longshot"),
    (0.15, 0.35, "underdog"),
    (0.35, 0.55, "coinflip"),
    (0.55, 1.01, "favorite"),
]

TIER_LABELS = {
    "longshot": "Longshot (<15%)",
    "underdog": "Underdog (15–35%)",
    "coinflip": "Coin flip (35–55%)",
    "favorite": "Favorite (55%+)",
}

MIN_TIER_SAMPLES = 5
TIER_ROI_PENALTY_THRESHOLD = -0.10
TIER_ROI_BONUS_THRESHOLD = 0.05

# Paper loose gates — flat floors (ignore env tier maps and ROI penalties)
_PAPER_LOOSE_MIN_EDGE = 0.005
_PAPER_LOOSE_MIN_PROB = 0.08

# Upset trap: crowd very confident but market prices longshot
UPSET_CONF_MIN = 0.65
UPSET_MARKET_MAX = 0.20

_SETTLED_CACHE: list[dict] | None = None


def invalidate_learning_cache() -> None:
    global _SETTLED_CACHE
    _SETTLED_CACHE = None


def price_tier(market_price: float) -> str:
    p = max(0.0, min(market_price, 1.0))
    for lo, hi, label in PRICE_TIERS:
        if lo <= p < hi:
            return label
    return "coinflip"


def is_upset_trap(raw_confidence: float | None, market_price: float) -> bool:
    conf = raw_confidence or 0.0
    return conf >= UPSET_CONF_MIN and market_price < UPSET_MARKET_MAX


def _empty_agg(label: str = "") -> dict[str, Any]:
    return {
        "settled": 0,
        "wins": 0,
        "total_staked": 0.0,
        "total_pnl": 0.0,
        "roi_pct": 0.0,
        "win_rate": 0.0,
        "label": label,
    }


def _finalize_agg(b: dict[str, Any]) -> None:
    n = b["settled"]
    if n:
        b["win_rate"] = round(b["wins"] / n, 4)
        if b["total_staked"] > 0:
            b["roi_pct"] = round(b["total_pnl"] / b["total_staked"] * 100, 2)


def _fetch_settled_autobets(db=None, *, use_cache: bool = True) -> list[dict]:
    global _SETTLED_CACHE
    if use_cache and _SETTLED_CACHE is not None:
        return _SETTLED_CACHE

    db = db or get_db()
    base_cols = (
        "id, status, stake, pnl, market_price, edge, model_prob, "
        "resolved_at, created_at, mode, match_id, matches(sport)"
    )
    extended_cols = base_cols.replace(
        "model_prob, ",
        "model_prob, raw_confidence, sport, ",
    )
    for cols in (extended_cols, base_cols):
        try:
            rows = (
                db.table("autobets")
                .select(cols)
                .in_("status", ["won", "lost"])
                .order("resolved_at")
                .execute()
                .data or []
            )
            break
        except Exception as exc:
            if cols == base_cols:
                logger.warning(f"Autobet learning fetch failed: {exc}")
                return []
            logger.debug(f"Autobet extended columns unavailable, using fallback: {exc}")
            rows = []
    else:
        rows = []

    for r in rows:
        if not r.get("sport") and r.get("matches"):
            r["sport"] = r["matches"].get("sport") or "football"
    if use_cache:
        _SETTLED_CACHE = rows
    return rows


def compute_tier_stats(db=None) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {
        label: {
            "tier": label,
            "label": TIER_LABELS[label],
            "settled": 0,
            "wins": 0,
            "total_staked": 0.0,
            "total_pnl": 0.0,
            "roi_pct": 0.0,
            "win_rate": 0.0,
            "avg_market_price": 0.0,
            "avg_edge": 0.0,
            "sharpe": None,
            "_returns": [],
        }
        for _, _, label in PRICE_TIERS
    }

    for r in _fetch_settled_autobets(db):
        tier = price_tier(r.get("market_price") or 0.5)
        b = buckets[tier]
        b["settled"] += 1
        if r.get("status") == "won":
            b["wins"] += 1
        stake = r.get("stake") or 0.0
        pnl = r.get("pnl") or 0.0
        b["total_staked"] += stake
        b["total_pnl"] += pnl
        b["avg_market_price"] += r.get("market_price") or 0.0
        b["avg_edge"] += r.get("edge") or 0.0
        if stake > 0:
            b["_returns"].append(pnl / stake)

    for b in buckets.values():
        n = b["settled"]
        if n:
            b["avg_market_price"] = round(b["avg_market_price"] / n, 4)
            b["avg_edge"] = round(b["avg_edge"] / n, 4)
            rets = b.pop("_returns")
            if len(rets) >= 3:
                mean_r = sum(rets) / len(rets)
                var = sum((x - mean_r) ** 2 for x in rets) / len(rets)
                std = math.sqrt(var) if var > 0 else 0.0
                b["sharpe"] = round(mean_r / std, 3) if std > 1e-9 else None
        else:
            b.pop("_returns", None)
        _finalize_agg(b)

    return buckets


def compute_sport_stats(db=None) -> dict[str, dict[str, Any]]:
    sports: dict[str, dict[str, Any]] = {}
    for r in _fetch_settled_autobets(db):
        sport = r.get("sport") or "football"
        if sport not in sports:
            sports[sport] = _empty_agg(sport)
        b = sports[sport]
        b["settled"] += 1
        if r.get("status") == "won":
            b["wins"] += 1
        b["total_staked"] += r.get("stake") or 0.0
        b["total_pnl"] += r.get("pnl") or 0.0
    for b in sports.values():
        _finalize_agg(b)
    return sports


def compute_upset_trap_stats(db=None) -> dict[str, Any]:
    trap = _empty_agg("High consensus + low market")
    other = _empty_agg("All other bets")
    for r in _fetch_settled_autobets(db):
        raw = r.get("raw_confidence")
        if raw is None:
            raw = r.get("model_prob")
        bucket = trap if is_upset_trap(raw, r.get("market_price") or 0.5) else other
        bucket["settled"] += 1
        if r.get("status") == "won":
            bucket["wins"] += 1
        bucket["total_staked"] += r.get("stake") or 0.0
        bucket["total_pnl"] += r.get("pnl") or 0.0
    _finalize_agg(trap)
    _finalize_agg(other)
    return {"upset_trap": trap, "normal": other}


def bankroll_curve(db=None) -> list[dict[str, Any]]:
    """Cumulative P&L after each settled autobet (paper track record)."""
    s = get_settings()
    curve: list[dict[str, Any]] = [{
        "at": None,
        "bankroll": round(s.polymarket_bankroll, 2),
        "pnl_cumulative": 0.0,
        "bet_n": 0,
    }]
    cumulative = 0.0
    for i, r in enumerate(_fetch_settled_autobets(db), start=1):
        cumulative += r.get("pnl") or 0.0
        curve.append({
            "at": r.get("resolved_at") or r.get("created_at"),
            "bankroll": round(s.polymarket_bankroll + cumulative, 2),
            "pnl_cumulative": round(cumulative, 2),
            "bet_n": i,
        })
    return curve


def assess_live_readiness(db=None) -> dict[str, Any]:
    """Live mode requires enough settled paper bets with positive ROI."""
    s = get_settings()
    rows = _fetch_settled_autobets(db)
    paper_rows = [r for r in rows if (r.get("mode") or "paper") == "paper"]
    if not paper_rows:
        # mode may not be in select — use all settled for paper track
        paper_rows = rows

    settled = len(rows)
    total_staked = sum(r.get("stake") or 0 for r in rows)
    total_pnl = sum(r.get("pnl") or 0 for r in rows)
    roi_pct = (total_pnl / total_staked * 100) if total_staked else 0.0

    min_n = s.polymarket_live_min_settled_bets
    min_roi = s.polymarket_live_min_roi_pct
    ready = settled >= min_n and roi_pct > min_roi

    reasons = []
    if settled < min_n:
        reasons.append(f"need {min_n - settled} more settled autobets ({settled}/{min_n})")
    if roi_pct <= min_roi:
        reasons.append(f"paper ROI {roi_pct:.1f}% must exceed {min_roi:.1f}%")

    return {
        "live_ready": ready,
        "settled_bets": settled,
        "min_settled_required": min_n,
        "paper_roi_pct": round(roi_pct, 2),
        "min_roi_required_pct": min_roi,
        "total_pnl": round(total_pnl, 2),
        "message": "Ready for live consideration" if ready else "; ".join(reasons),
    }


@dataclass
class TierGates:
    tier: str
    min_edge: float
    min_model_prob: float
    adjusted: bool = False
    note: str = ""


def _base_gates(tier: str, *, paper: bool) -> tuple[float, float]:
    s = get_settings()
    if paper:
        edge_map = {
            "longshot": s.polymarket_longshot_min_edge_paper,
            "underdog": s.polymarket_underdog_min_edge_paper,
            "coinflip": s.polymarket_paper_min_edge,
            "favorite": s.polymarket_paper_min_edge,
        }
        prob_map = {
            "longshot": s.polymarket_longshot_min_model_prob,
            "underdog": s.polymarket_underdog_min_model_prob,
            "coinflip": s.polymarket_coinflip_min_model_prob,
            "favorite": s.polymarket_favorite_min_model_prob,
        }
    else:
        edge_map = {
            "longshot": s.polymarket_longshot_min_edge_live,
            "underdog": s.polymarket_underdog_min_edge_live,
            "coinflip": s.polymarket_min_edge,
            "favorite": s.polymarket_min_edge,
        }
        prob_map = {
            "longshot": s.polymarket_longshot_min_model_prob,
            "underdog": s.polymarket_underdog_min_model_prob,
            "coinflip": s.polymarket_coinflip_min_model_prob,
            "favorite": s.polymarket_favorite_min_model_prob,
        }
    return (
        edge_map.get(tier, s.polymarket_paper_min_edge if paper else s.polymarket_min_edge),
        prob_map.get(tier, 0.25),
    )


def gates_for_price(
    market_price: float,
    *,
    paper: bool,
    raw_confidence: float | None = None,
    sport: str | None = None,
) -> TierGates:
    tier = price_tier(market_price)
    min_edge, min_prob = _base_gates(tier, paper=paper)
    notes: list[str] = []
    adjusted = False
    s = get_settings()

    if paper and s.polymarket_paper_loose_gates:
        min_edge = _PAPER_LOOSE_MIN_EDGE
        min_prob = _PAPER_LOOSE_MIN_PROB
        notes.append("paper loose gates")
        if sport == "mlb":
            notes.append("mlb bootstrap")
        return TierGates(
            tier=tier,
            min_edge=min_edge,
            min_model_prob=min_prob,
            adjusted=True,
            note="; ".join(notes),
        )

    tier_stats = compute_tier_stats().get(tier, {})
    if (tier_stats.get("settled") or 0) >= MIN_TIER_SAMPLES:
        roi_frac = (tier_stats.get("roi_pct") or 0) / 100.0
        if roi_frac <= TIER_ROI_PENALTY_THRESHOLD:
            min_edge = min(0.15, min_edge + 0.015)
            min_prob = min(0.65, min_prob + 0.05)
            adjusted = True
            notes.append(f"{tier} ROI {tier_stats['roi_pct']:.1f}%")
        elif roi_frac >= TIER_ROI_BONUS_THRESHOLD and tier_stats["settled"] >= MIN_TIER_SAMPLES * 2:
            floor = get_settings().polymarket_paper_min_edge if paper else get_settings().polymarket_min_edge
            min_edge = max(min_edge - 0.005, floor)
            adjusted = True
            notes.append(f"{tier} ROI +{tier_stats['roi_pct']:.1f}% relaxed")

    upset = compute_upset_trap_stats()
    trap = upset["upset_trap"]
    if is_upset_trap(raw_confidence, market_price) and (trap.get("settled") or 0) >= MIN_TIER_SAMPLES:
        trap_roi = (trap.get("roi_pct") or 0) / 100.0
        if trap_roi <= TIER_ROI_PENALTY_THRESHOLD:
            min_edge = min(0.12, min_edge + 0.02)
            min_prob = min(0.70, min_prob + 0.08)
            adjusted = True
            notes.append(f"upset-trap ROI {trap['roi_pct']:.1f}%")

    if sport:
        sport_stats = compute_sport_stats().get(sport, {})
        if (sport_stats.get("settled") or 0) >= MIN_TIER_SAMPLES:
            s_roi = (sport_stats.get("roi_pct") or 0) / 100.0
            if s_roi <= TIER_ROI_PENALTY_THRESHOLD:
                min_edge = min(0.12, min_edge + 0.01)
                adjusted = True
                notes.append(f"{sport} ROI {sport_stats['roi_pct']:.1f}%")
        elif paper and sport == "mlb":
            # No MLB paper track record yet — crowd picks often hug the market.
            min_edge = max(0.015, min_edge - 0.012)
            min_prob = max(0.20, min_prob - 0.05)
            adjusted = True
            notes.append("mlb paper bootstrap")

    if adjusted:
        logger.debug(f"Autobet gates [{tier}]: {', '.join(notes)}")

    return TierGates(
        tier=tier,
        min_edge=round(min_edge, 4),
        min_model_prob=round(min_prob, 4),
        adjusted=adjusted,
        note="; ".join(notes),
    )


def learning_summary(*, paper: bool) -> dict[str, Any]:
    invalidate_learning_cache()
    tier_stats = compute_tier_stats()
    midpoints = {"longshot": 0.08, "underdog": 0.25, "coinflip": 0.45, "favorite": 0.65}
    active_gates = {
        label: {
            **gates_for_price(midpoints[label], paper=paper).__dict__,
        }
        for _, _, label in PRICE_TIERS
    }

    return {
        "tier_stats": tier_stats,
        "sport_stats": compute_sport_stats(),
        "upset_trap": compute_upset_trap_stats(),
        "bankroll_curve": bankroll_curve(),
        "live_readiness": assess_live_readiness(),
        "active_gates": active_gates,
        "min_tier_samples": MIN_TIER_SAMPLES,
    }
