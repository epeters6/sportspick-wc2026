from fastapi import APIRouter
from backend.db import get_db
import math
from typing import List, Dict, Any

router = APIRouter(prefix="/models", tags=["models"])

def calculate_brier_score(bets: List[Dict[str, Any]]) -> float:
    if not bets:
        return 0.0
    
    squared_errors = []
    for b in bets:
        if b.get('status') not in ('won', 'lost'):
            continue
        prob = b.get('model_prob', 0.0)
        actual = 1.0 if b.get('status') == 'won' else 0.0
        squared_errors.append((prob - actual) ** 2)
        
    if not squared_errors:
        return 0.0
    return sum(squared_errors) / len(squared_errors)

@router.get("/overview")
def get_models_overview():
    db = get_db()
    
    # We will aggregate models based on their domains/sports:
    # consensus: sport in ('football', 'wc2026')
    # weather: sport = 'weather'
    # mlb: sport = 'baseball'
    # soccer: sport = 'soccer'
    
    model_configs = [
        {"id": "consensus", "name": "Consensus Engine", "sports": ["football", "wc2026", "ncaaf"]},
        {"id": "weather", "name": "Weather Ensembles", "sports": ["weather"]},
        {"id": "mlb", "name": "Pavlov MLB", "sports": ["baseball"]},
        {"id": "soccer", "name": "Soccer Matchups", "sports": ["soccer"]},
    ]
    
    results = []
    for cfg in model_configs:
        bets = db.table('autobets').select('status, pnl, stake, model_prob').in_('sport', cfg['sports']).execute().data or []
        
        resolved_bets = [b for b in bets if b.get('status') in ('won', 'lost')]
        won_bets = [b for b in resolved_bets if b.get('status') == 'won']
        
        total_pnl = sum(float(b.get('pnl') or 0.0) for b in resolved_bets)
        total_staked = sum(float(b.get('stake') or 0.0) for b in resolved_bets)
        
        roi = (total_pnl / total_staked) if total_staked > 0 else 0.0
        win_rate = (len(won_bets) / len(resolved_bets)) if len(resolved_bets) > 0 else 0.0
        brier = calculate_brier_score(resolved_bets)
        
        results.append({
            "id": cfg["id"],
            "name": cfg["name"],
            "total_trades": len(resolved_bets),
            "win_rate": win_rate,
            "roi": roi,
            "brier_score": brier,
        })
        
    return results

@router.get("/calibration")
def get_model_calibration():
    db = get_db()
    logs = db.table('calibration_logs').select('*').order('created_at', desc=True).limit(50).execute().data or []
    return logs

@router.get("/readiness")
def get_model_readiness():
    # Uses the same calculation as overview to build the readiness checklist
    overview = get_models_overview()
    results = {}
    
    for m in overview:
        trades_ok = m["total_trades"] >= 100
        roi_ok = m["roi"] > 0
        brier_ok = m["brier_score"] < 0.22 and m["total_trades"] > 0
        
        score = 0
        if trades_ok: score += 33
        if roi_ok: score += 34
        if brier_ok: score += 33
        
        results[m["id"]] = {
            "score": score,
            "criteria": {
                "sample_size": {"met": trades_ok, "value": m["total_trades"], "threshold": 100},
                "roi": {"met": roi_ok, "value": m["roi"], "threshold": 0.0},
                "brier_score": {"met": brier_ok, "value": m["brier_score"], "threshold": 0.22}
            },
            "ready": trades_ok and roi_ok and brier_ok
        }
        
    return results
