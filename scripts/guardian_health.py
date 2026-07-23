import os
import json
import logging
from datetime import datetime, timedelta, timezone
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.db import get_db
from backend.config import get_settings

logger = logging.getLogger(__name__)

HALT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".guardian_halt.json")


def settlement_risk_summary(rows: list[dict]) -> tuple[dict[str, float], float, bool]:
    """Conservative realized P&L plus the MLB identity-integrity signal."""
    from backend.trading.settlement_integrity import (
        SettlementCheck,
        conservative_risk_pnl,
        verify_match_linked_autobet,
    )

    by_sport: dict[str, float] = {}
    total_pnl = 0.0
    mlb_integrity_failed = False
    for row in rows:
        sport = (row.get("sport") or "unknown").split("_")[0]
        match = row.get("matches")
        if isinstance(match, list):
            match = match[0] if len(match) == 1 else None
        if isinstance(match, dict):
            check = verify_match_linked_autobet(row, match)
        else:
            check = SettlementCheck(
                False,
                "SETTLEMENT_EVIDENCE_UNAVAILABLE",
                None,
                None,
                None,
                None,
            )
        if (
            row.get("match_id")
            and ("mlb" in sport.lower() or "baseball" in sport.lower())
            and not check.valid
        ):
            mlb_integrity_failed = True
        pnl = conservative_risk_pnl(row, check)
        by_sport[sport] = by_sport.get(sport, 0.0) + pnl
        total_pnl += pnl
    return by_sport, total_pnl, mlb_integrity_failed


def check_health(*, emit: bool = True):
    db = get_db()
    s = get_settings()
    
    halt_reasons = []
    
    # 1. Pipeline Health Monitor (Stale Data)
    try:
        influencers = db.table("influencers").select("last_scraped_at").execute().data or []
        if influencers:
            # Parse timestamps and find the most recent
            recent_times = []
            for inf in influencers:
                t_str = inf.get("last_scraped_at")
                if t_str:
                    try:
                        # Some formats might have fractional seconds or timezone info
                        dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                        # Ensure dt is timezone unaware in UTC for comparison, or just use now(timezone.utc)
                        recent_times.append(dt)
                    except Exception:
                        pass
                        
            if recent_times:
                latest = max(recent_times)
                # If latest is naive, assume UTC. If aware, compare with aware UTC now
                now = datetime.now(latest.tzinfo) if latest.tzinfo else datetime.utcnow()
                if (now - latest).total_seconds() > 24 * 3600:
                    halt_reasons.append(f"Stale Data: Latest influencer scrape was {(now - latest).total_seconds() / 3600:.1f} hours ago.")
            else:
                halt_reasons.append("Stale Data: No valid last_scraped_at timestamps found.")
    except Exception as exc:
        logger.error(f"Guardian failed to check influencer health: {exc}")
        
    # 2. Guardian Risk Circuit-Breaker: CLV Degradation (with Shrinkage)
    try:
        # Fetch autobets with CLV over the last 14 days
        fourteen_days_ago = (datetime.utcnow() - timedelta(days=14)).isoformat()
        recent_bets = (
            db.table("autobets")
            .select("sport, stake, market_price, closing_price")
            .gte("created_at", fourteen_days_ago)
            .not_.is_("closing_price", "null")
            .execute()
            .data or []
        )
        
        # We group by domain/sport, and also track portfolio aggregate
        domains = {}
        portfolio_ev = 0.0
        portfolio_staked = 0.0
        portfolio_count = 0
        
        for b in recent_bets:
            sport = b.get("sport") or "unknown"
            stake = float(b.get("stake") or 0.0)
            placed = float(b.get("market_price") or 0.0)
            closing = float(b.get("closing_price") or 0.0)
            
            if placed <= 0 or closing <= 0 or stake <= 0:
                continue
                
            payout = stake / placed
            ev = (payout * closing) - stake
            
            if sport not in domains:
                domains[sport] = {"ev": 0.0, "staked": 0.0, "count": 0}
                
            domains[sport]["ev"] += ev
            domains[sport]["staked"] += stake
            domains[sport]["count"] += 1
            
            portfolio_ev += ev
            portfolio_staked += stake
            portfolio_count += 1
            
        def apply_shrinkage(observed_roi: float, n_bets: int, prior_roi: float = 0.0, k: float = 20.0) -> float:
            """Shrinks observed ROI toward prior (0%) when sample size (n_bets) is small."""
            weight = n_bets / (n_bets + k)
            return (weight * observed_roi) + ((1 - weight) * prior_roi)

        # Domain-Level Circuit Breakers
        for sport, data in domains.items():
            if data["count"] > 0 and data["staked"] > 0:
                raw_roi = data["ev"] / data["staked"]
                shrunken_roi = apply_shrinkage(raw_roi, data["count"])
                
                if shrunken_roi < -0.03: # Trailing Shrunken ROI < -3%
                    halt_reasons.append(f"Domain Breaker [{sport}]: Shrunken Expected ROI is {shrunken_roi:.2%} (Raw: {raw_roi:.2%}, N={data['count']}).")
                    
        # Portfolio-Level Circuit Breaker
        if portfolio_count > 0 and portfolio_staked > 0:
            raw_port_roi = portfolio_ev / portfolio_staked
            shrunken_port_roi = apply_shrinkage(raw_port_roi, portfolio_count, k=50.0)
            if shrunken_port_roi < -0.05: # Portfolio aggregate drops < -5%
                halt_reasons.append(f"Portfolio Breaker: Aggregate Shrunken Expected ROI is {shrunken_port_roi:.2%} (N={portfolio_count}). System-wide halt.")
                
    except Exception as exc:
        logger.error(f"Guardian failed to check CLV degradation: {exc}")

    # 3. Realized-loss hard stops (apply even when paper_loose_gates=true)
    #    - tier/sport: >5% of bankroll lost over 7 days → halt that sport
    #    - account: >12% of bankroll lost over 7 days → halt all automated betting
    try:
        from backend.trading.autobet import _current_bankroll

        bankroll = float(_current_bankroll(db) or 0.0)
        seven_days_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
        settled = (
            db.table("autobets")
            .select(
                "id, match_id, sport, outcome_name, bet_type, bet_line, bet_subject, "
                "status, pnl, stake, shares, market_price, resolved_at, "
                "settlement_version, settlement_match_id, settlement_corrected_at, "
                "matches:matches!autobets_match_id_fkey("
                "id, sport, external_id, home_team, away_team, scheduled_at, "
                "finished_at, winner, is_final, home_score, away_score, match_stats)"
            )
            .gte("resolved_at", seven_days_ago)
            .in_("status", ["won", "lost"])
            .execute()
            .data
            or []
        )
        by_sport, total_pnl, mlb_integrity_failed = settlement_risk_summary(
            settled
        )

        if mlb_integrity_failed:
            halt_reasons.append("SETTLEMENT_INTEGRITY_HALT [mlb]")

        if bankroll > 0:
            for sport, pnl in by_sport.items():
                if pnl < 0 and abs(pnl) / bankroll > 0.05:
                    halt_reasons.append(
                        f"TIER_HARD_STOP [{sport}]: 7d realized loss {pnl:.2f} "
                        f"exceeds 5% of bankroll ({bankroll:.2f})."
                    )
            if total_pnl < 0 and abs(total_pnl) / bankroll > 0.12:
                halt_reasons.append(
                    f"ACCOUNT_HARD_STOP: 7d realized loss {total_pnl:.2f} "
                    f"exceeds 12% of bankroll ({bankroll:.2f})."
                )
    except Exception as exc:
        logger.error(f"Guardian failed realized-loss hard-stop check: {exc}")

    # 4. Persist halt state (local file + durable app_settings so new runners see it)
    state = {
        "halted": len(halt_reasons) > 0,
        "reasons": halt_reasons,
        "updated_at": datetime.utcnow().isoformat(),
        "requires_explicit_resume": True if halt_reasons else False,
    }

    # Do not auto-clear a prior durable halt without explicit resume
    try:
        prior = (
            db.table("app_settings")
            .select("value")
            .eq("key", "guardian_halt")
            .execute()
            .data
            or []
        )
        if prior:
            prev = prior[0].get("value") or {}
            if prev.get("halted") and prev.get("requires_explicit_resume"):
                # Merge prior reasons; keep halted until audited resume
                merged = list(dict.fromkeys((prev.get("reasons") or []) + halt_reasons))
                state = {
                    "halted": True,
                    "reasons": merged,
                    "updated_at": datetime.utcnow().isoformat(),
                    "requires_explicit_resume": True,
                    "prior_halt_preserved": True,
                }
    except Exception as exc:
        logger.warning(f"Guardian durable read failed: {exc}")

    with open(HALT_FILE, "w") as f:
        json.dump(state, f, indent=2)

    try:
        db.table("app_settings").upsert(
            {
                "key": "guardian_halt",
                "value": state,
                "updated_at": datetime.utcnow().isoformat(),
            },
            on_conflict="key",
        ).execute()
    except Exception as exc:
        logger.error(f"Guardian durable write failed (required): {exc}")
        raise

    if emit and state["halted"]:
        print(f"⚠️ GUARDIAN HALT TRIGGERED:")
        for r in state["reasons"]:
            print(f"  - {r}")
        print(
            "\nRESUMPTION RULE: Halt persists across runners. "
            "Requires explicit audited resume after hard stop."
        )
    elif emit:
        print("✅ Guardian Health Check Passed.")

    return state


if __name__ == "__main__":
    check_health()
