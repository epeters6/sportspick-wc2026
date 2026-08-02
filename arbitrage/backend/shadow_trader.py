"""Conservative paper-entry policy for verified executable arbitrage."""
from __future__ import annotations

import logging
from typing import List

from .config import settings
from .database import log_shadow_bet
from .models import ArbitrageOpportunity

log = logging.getLogger(__name__)


def process_shadow_bets(opps: List[ArbitrageOpportunity]) -> int:
    """Open paper positions only from hard-matched, fresh, liquid quotes."""
    placed_count = 0
    for opp in opps:
        reasons = set(opp.match_reasons or [])
        if (
            not opp.is_profitable
            or not opp.quote_valid
            or "hard_identity_passed" not in reasons
            or opp.match_confidence < 85.0
            or opp.net_gap < 0.005
            or opp.roi_pct < 0.5
            or opp.executable_size < settings.min_executable_contracts
        ):
            continue

        requested_size = min(settings.shadow_max_contracts, int(opp.executable_size))
        bet_id = log_shadow_bet(opp, size_contracts=requested_size)
        if bet_id:
            placed_count += 1
            log.info(
                "Paper bet #%d reserved: %s | %.2f contracts | $%.2f expected, unrealized",
                bet_id,
                opp.event_name[:60],
                min(requested_size, opp.executable_size),
                opp.net_gap * min(requested_size, opp.executable_size),
            )
    return placed_count
