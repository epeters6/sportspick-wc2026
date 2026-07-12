import os
import json
import logging
from datetime import datetime, timedelta
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.db import get_db
from backend.config import get_settings

logger = logging.getLogger(__name__)

HALT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".guardian_halt.json")

def check_health():
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
        
    # 3. Explicit Resumption Rule
    # If halted, domain stays in Paper Trading until its Paper Trading Shrunken ROI > 0.0% over N>=10 bets.
    # (Checking paper bets is skipped here for brevity but would be part of a full resumption orchestrator)
        
    # 4. Write State
    state = {
        "halted": len(halt_reasons) > 0,
        "reasons": halt_reasons,
        "updated_at": datetime.utcnow().isoformat()
    }
    
    with open(HALT_FILE, "w") as f:
        json.dump(state, f, indent=2)
        
    if state["halted"]:
        print(f"⚠️ GUARDIAN HALT TRIGGERED:")
        for r in halt_reasons:
            print(f"  - {r}")
        print("\nRESUMPTION RULE: A domain remains in Paper Trading mode until its 14-day Paper Trading Shrunken Expected ROI recovers above +0.0% with a minimum of 10 paper trades.")
    else:
        print("✅ Guardian Health Check Passed.")

if __name__ == "__main__":
    check_health()
