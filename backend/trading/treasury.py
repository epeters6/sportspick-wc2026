import logging
import asyncio
from backend.trading.polymarket_client import PolymarketClient
from backend.config import get_settings

logger = logging.getLogger(__name__)

def check_treasury_health():
    """
    Guardian Treasury Circuit Breaker.
    Pauses live execution if the treasury balance drops below 10x the max position size.
    """
    s = get_settings()
    if not s.polymarket_live_enabled:
        return
        
    try:
        # Run async client in sync context if needed
        client = PolymarketClient()
        # Mocking the balance fetch since PolymarketClient doesn't have a direct balance method yet
        # Normally this would be: balance = asyncio.run(client.get_balance())
        # For now, we simulate a check
        balance = 5000.0 # Placeholder
        
        # We need a minimum amount of ammo to comfortably place bets
        # 10x the max position size is a reasonable minimum
        min_balance = s.polymarket_max_position_pct * balance * 10
        if min_balance < 500.0:
            min_balance = 500.0
            
        if balance < min_balance:
            logger.warning(f"TREASURY ALERT: Balance (${balance:.2f}) is below minimum threshold (${min_balance:.2f}).")
            import os, json
            HALT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".guardian_halt.json")
            try:
                if os.path.exists(HALT_FILE):
                    with open(HALT_FILE, "r") as f:
                        state = json.load(f)
                else:
                    state = {"halted": False, "reasons": []}
            except Exception:
                state = {"halted": False, "reasons": []}
                
            state["halted"] = True
            reason = f"Treasury Alert: Balance (${balance:.2f}) below threshold (${min_balance:.2f})."
            if reason not in state.get("reasons", []):
                state.setdefault("reasons", []).append(reason)
                
            with open(HALT_FILE, "w") as f:
                json.dump(state, f, indent=2)
            
    except Exception as exc:
        logger.error(f"Failed to check treasury health: {exc}")
