"""
Risk engine — converts an edge into a safe position size, or rejects the bet.

Layered defences (a bet must clear EVERY gate):
  1. Kill switch         — global enable flag (paper mode bets are always allowed
                           to be *recorded*; only live execution is gated).
  2. Price sanity        — refuse near-0 / near-1 prices (no edge, huge variance).
  3. Minimum edge        — refuse anything below the configured edge floor.
  4. Fractional Kelly     — size with quarter-Kelly by default, never full Kelly.
  5. Per-position cap     — clamp to a fraction of bankroll.
  6. Per-event cap        — limit total stake across one match/event (correlation).
  7. Total exposure cap   — limit total open stake across the whole book.
  8. Liquidity / slippage — never take more than a fraction of available depth,
                           and skip markets thinner than a liquidity floor.

The output is a `Sizing` decision the orchestrator can act on directly.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from backend.config import get_settings


@dataclass
class Sizing:
    approved: bool
    stake: float                # USDC to stake (0 if rejected)
    kelly_fraction: float       # raw (pre-cap) fractional-Kelly value
    reject_reason: str | None = None


def kelly_fraction_binary(p: float, price: float) -> float:
    """
    Kelly fraction for a binary prediction-market contract.

    A share costs `price` and pays $1 if the outcome occurs. In Kelly terms:
      b = (1 - price) / price       # net odds (profit per unit staked)
      f* = (b·p - (1-p)) / b  = (p - price) / (1 - price)

    The tidy closed form for a 0/1 payout contract is:
        f* = (p - price) / (1 - price)
    which is positive exactly when p > price (i.e. we have edge).
    """
    if price <= 0 or price >= 1:
        return 0.0
    f = (p - price) / (1 - price)
    return max(0.0, f)


def size_position(
    *,
    model_prob: float,
    market_price: float,
    edge: float,
    bankroll: float,
    current_total_exposure: float,
    current_event_exposure: float,
    book_depth_usdc: float,
    min_edge: float | None = None,
    min_model_prob: float | None = None,
    min_liquidity: float | None = None,
    paper: bool = False,
) -> Sizing:
    """
    Decide whether to bet and how much. Returns a Sizing decision.

    current_total_exposure  — sum of open stakes across the whole book
    current_event_exposure  — sum of open stakes on this match/event already
    book_depth_usdc         — estimated takeable liquidity at the top of book
    """
    s = get_settings()

    # ── Gate 2: price sanity ──────────────────────────────────────────────────
    if not (s.polymarket_min_price <= market_price <= s.polymarket_max_price):
        return Sizing(False, 0.0, 0.0,
                      f"price {market_price:.3f} outside "
                      f"[{s.polymarket_min_price}, {s.polymarket_max_price}]")

    # ── Gate 3: minimum edge ──────────────────────────────────────────────────
    edge_floor = min_edge if min_edge is not None else s.polymarket_min_edge
    if edge < edge_floor:
        return Sizing(False, 0.0, 0.0,
                      f"edge {edge:.3f} < min {edge_floor}")

    # ── Gate 3b: minimum blended win probability (price-tier learning) ────────
    if min_model_prob is not None and model_prob < min_model_prob:
        return Sizing(False, 0.0, 0.0,
                      f"model prob {model_prob:.2f} < min {min_model_prob:.2f}")

    # ── Gate 4: fractional Kelly ──────────────────────────────────────────────
    raw_kelly = kelly_fraction_binary(model_prob, market_price)
    if raw_kelly <= 0:
        return Sizing(False, 0.0, 0.0, "non-positive Kelly")
    frac_kelly = raw_kelly * s.polymarket_kelly_multiplier
    stake = bankroll * frac_kelly

    if paper:
        position_pct = s.polymarket_paper_max_position_pct
        event_pct = s.polymarket_paper_max_event_exposure_pct
        total_pct = s.polymarket_paper_max_total_exposure_pct
    else:
        position_pct = s.polymarket_max_position_pct
        event_pct = s.polymarket_max_event_exposure_pct
        total_pct = s.polymarket_max_total_exposure_pct

    # ── Gate 5: per-position cap ──────────────────────────────────────────────
    position_cap = bankroll * position_pct
    stake = min(stake, position_cap)

    # ── Gate 6: per-event (correlation) cap ───────────────────────────────────
    if event_pct < 1.0:
        event_cap = bankroll * event_pct
        event_room = max(0.0, event_cap - current_event_exposure)
        if event_room <= 0:
            return Sizing(False, 0.0, raw_kelly,
                          f"event exposure cap reached (${current_event_exposure:.0f})")
        stake = min(stake, event_room)

    # ── Gate 7: total exposure cap ────────────────────────────────────────────
    if total_pct < 1.0:
        total_cap = bankroll * total_pct
        total_room = max(0.0, total_cap - current_total_exposure)
        if total_room <= 0:
            return Sizing(False, 0.0, raw_kelly,
                          f"total exposure cap reached (${current_total_exposure:.0f})")
        stake = min(stake, total_room)

    # ── Gate 8: liquidity / slippage ──────────────────────────────────────────
    liq_floor = min_liquidity if min_liquidity is not None else s.polymarket_min_liquidity
    if book_depth_usdc < liq_floor:
        return Sizing(False, 0.0, raw_kelly,
                      f"insufficient liquidity (${book_depth_usdc:.0f} "
                      f"< ${liq_floor})")
    depth_cap = book_depth_usdc * s.polymarket_max_book_pct
    stake = min(stake, depth_cap)

    # Round and enforce a $1 minimum
    stake = round(stake, 2)
    if stake < 1.0:
        return Sizing(False, 0.0, raw_kelly, f"stake ${stake:.2f} below $1 minimum")

    return Sizing(True, stake, round(raw_kelly, 4), None)
