"""Pure, fail-closed verification for durable bet settlements."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.sports_data.bet_settlement import pick_won_for_autobet
from backend.trading.market_matcher import _canonical

SETTLEMENT_VERSION = "exact_match_v2"
WEATHER_SETTLEMENT_VERSION = "weather_actual_v2"

EXACT_MATCH_NOT_FOUND = "EXACT_MATCH_NOT_FOUND"
EXACT_MATCH_NOT_FINAL = "EXACT_MATCH_NOT_FINAL"
PRESTART_SETTLEMENT_BLOCK = "PRESTART_SETTLEMENT_BLOCK"
INVALID_MATCH_WINNER = "INVALID_MATCH_WINNER"
SETTLEMENT_DATA_INCOMPLETE = "SETTLEMENT_DATA_INCOMPLETE"
SETTLEMENT_STATUS_MISMATCH = "SETTLEMENT_STATUS_MISMATCH"
SETTLEMENT_PNL_MISMATCH = "SETTLEMENT_PNL_MISMATCH"


@dataclass(frozen=True)
class SettlementCheck:
    valid: bool
    reason: str | None
    expected_status: str | None
    expected_pnl: float | None
    scheduled_at: datetime | None
    settlement_match_id: str | None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse common database timestamps and normalize them to aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def expected_autobet_pnl(
    *,
    won: bool,
    stake: float,
    shares: float,
    market_price: float,
) -> float:
    if won:
        return round(shares * (1.0 - market_price), 2)
    return round(-stake, 2)


def _invalid(
    reason: str,
    *,
    scheduled_at: datetime | None = None,
    match_id: Any = None,
    expected_status: str | None = None,
    expected_pnl: float | None = None,
) -> SettlementCheck:
    return SettlementCheck(
        valid=False,
        reason=reason,
        expected_status=expected_status,
        expected_pnl=expected_pnl,
        scheduled_at=scheduled_at,
        settlement_match_id=str(match_id) if match_id is not None else None,
    )


def _canonical_team(value: Any) -> str:
    raw = str(value or "").strip()
    return (_canonical(raw) or raw.lower()).strip()


def verify_match_linked_autobet(
    bet: dict,
    match: dict,
    *,
    now: datetime | None = None,
) -> SettlementCheck:
    """Verify one stored settlement using only its exact linked match."""
    bet_match_id = bet.get("match_id")
    match_id = match.get("id") if isinstance(match, dict) else None
    if not bet_match_id or not match_id or str(bet_match_id) != str(match_id):
        return _invalid(EXACT_MATCH_NOT_FOUND, match_id=match_id)

    scheduled_at = parse_timestamp(match.get("scheduled_at"))
    if scheduled_at is None:
        return _invalid(
            SETTLEMENT_DATA_INCOMPLETE,
            match_id=match_id,
        )
    if match.get("is_final") is not True:
        return _invalid(
            EXACT_MATCH_NOT_FINAL,
            scheduled_at=scheduled_at,
            match_id=match_id,
        )

    check_now = parse_timestamp(now) or datetime.now(timezone.utc)
    if check_now < scheduled_at:
        return _invalid(
            PRESTART_SETTLEMENT_BLOCK,
            scheduled_at=scheduled_at,
            match_id=match_id,
        )

    bet_type = str(bet.get("bet_type") or "moneyline").lower()
    if bet_type in ("moneyline", "draw"):
        actual = _canonical_team(match.get("winner"))
        home = _canonical_team(match.get("home_team"))
        away = _canonical_team(match.get("away_team"))
        sport = str(match.get("sport") or bet.get("sport") or "").lower()
        valid_winners = {home, away}
        if bet_type == "draw" or ("mlb" not in sport and "baseball" not in sport):
            valid_winners.add("draw")
        if not actual or actual not in valid_winners:
            return _invalid(
                INVALID_MATCH_WINNER,
                scheduled_at=scheduled_at,
                match_id=match_id,
            )

    won = pick_won_for_autobet(
        bet_type=bet_type,
        outcome_name=bet.get("outcome_name") or "",
        bet_line=bet.get("bet_line"),
        bet_subject=bet.get("bet_subject"),
        match=match,
        match_stats=match.get("match_stats"),
    )
    if won is None:
        return _invalid(
            SETTLEMENT_DATA_INCOMPLETE,
            scheduled_at=scheduled_at,
            match_id=match_id,
        )

    expected_status = "won" if won else "lost"
    try:
        expected_pnl = expected_autobet_pnl(
            won=won,
            stake=float(bet.get("stake") or 0.0),
            shares=float(bet.get("shares") or 0.0),
            market_price=float(bet.get("market_price") or 0.0),
        )
    except (TypeError, ValueError):
        return _invalid(
            SETTLEMENT_DATA_INCOMPLETE,
            scheduled_at=scheduled_at,
            match_id=match_id,
            expected_status=expected_status,
        )

    resolved_at = parse_timestamp(bet.get("resolved_at"))
    if resolved_at is not None and resolved_at < scheduled_at:
        corrected_at = parse_timestamp(bet.get("settlement_corrected_at"))
        historical_reverified = (
            bet.get("settlement_version") == SETTLEMENT_VERSION
            and corrected_at is not None
            and corrected_at >= scheduled_at
        )
        if not historical_reverified:
            return _invalid(
                PRESTART_SETTLEMENT_BLOCK,
                scheduled_at=scheduled_at,
                match_id=match_id,
                expected_status=expected_status,
                expected_pnl=expected_pnl,
            )
    if bet.get("status") != expected_status:
        return _invalid(
            SETTLEMENT_STATUS_MISMATCH,
            scheduled_at=scheduled_at,
            match_id=match_id,
            expected_status=expected_status,
            expected_pnl=expected_pnl,
        )
    try:
        stored_pnl = float(bet.get("pnl"))
    except (TypeError, ValueError):
        return _invalid(
            SETTLEMENT_PNL_MISMATCH,
            scheduled_at=scheduled_at,
            match_id=match_id,
            expected_status=expected_status,
            expected_pnl=expected_pnl,
        )
    if abs(stored_pnl - expected_pnl) > 0.0100001:
        return _invalid(
            SETTLEMENT_PNL_MISMATCH,
            scheduled_at=scheduled_at,
            match_id=match_id,
            expected_status=expected_status,
            expected_pnl=expected_pnl,
        )

    return SettlementCheck(
        valid=True,
        reason=None,
        expected_status=expected_status,
        expected_pnl=expected_pnl,
        scheduled_at=scheduled_at,
        settlement_match_id=str(match_id),
    )


def conservative_risk_pnl(bet: dict, check: SettlementCheck) -> float:
    if check.valid:
        return float(check.expected_pnl)
    return min(
        float(bet.get("pnl") or 0.0),
        -abs(float(bet.get("stake") or 0.0)),
    )
