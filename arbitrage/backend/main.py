"""Kalshi/Polymarket research scanner with executable-price integrity."""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .calculator import calculate_all
from .config import (
    KALSHI_MAKER_RATE,
    KALSHI_TAKER_RATE,
    POLYMARKET_TAKER_RATES,
    kalshi_fee_per_contract,
    polymarket_fee_per_share,
    settings,
)
from .database import (
    DATA_VERSION,
    finish_scan,
    get_history_stats,
    get_shadow_bets_summary,
    init_db,
    log_opportunities,
    prune_history,
    start_scan,
)
from .execution import hydrate_executable_quotes
from .kalshi_client import fetch_kalshi_markets
from .matcher import find_matches
from .models import ArbitrageOpportunity
from .polymarket_client import fetch_polymarket_markets
from .settlement import settle_ready_shadow_bets
from .shadow_trader import process_shadow_bets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-24s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arbitrage")

_latest_opportunities: List[ArbitrageOpportunity] = []
_last_refresh: Optional[str] = None
_last_error: Optional[str] = None
_scan_count = 0
_refresh_lock = asyncio.Lock()


async def _refresh_data() -> None:
    """Run one fail-closed discovery, identity, quote, and paper cycle."""
    global _latest_opportunities, _last_refresh, _last_error, _scan_count

    async with _refresh_lock:
        scan_id = await asyncio.to_thread(start_scan)
        kalshi_count = poly_count = match_count = 0
        try:
            async with httpx.AsyncClient(
                headers={"Accept": "application/json"},
                follow_redirects=True,
            ) as client:
                kalshi_markets, poly_markets = await asyncio.gather(
                    fetch_kalshi_markets(client),
                    fetch_polymarket_markets(client),
                    return_exceptions=True,
                )
                if isinstance(kalshi_markets, Exception):
                    raise RuntimeError(f"Kalshi fetch failed: {kalshi_markets}")
                if isinstance(poly_markets, Exception):
                    raise RuntimeError(f"Polymarket fetch failed: {poly_markets}")
                if not kalshi_markets or not poly_markets:
                    raise RuntimeError(
                        f"incomplete venue data: Kalshi={len(kalshi_markets)}, "
                        f"Polymarket={len(poly_markets)}"
                    )

                kalshi_count = len(kalshi_markets)
                poly_count = len(poly_markets)
                minimum = 0.01
                kalshi_markets = [
                    market for market in kalshi_markets
                    if market.yes_price >= minimum or market.no_price >= minimum
                ]
                poly_markets = [
                    market for market in poly_markets
                    if market.yes_price >= minimum or market.no_price >= minimum
                ]

                matches = find_matches(
                    kalshi_markets,
                    poly_markets,
                    auto_threshold=settings.match_auto_threshold,
                    review_threshold=settings.match_review_threshold,
                )
                match_count = len(matches)
                await hydrate_executable_quotes(client, matches)
                opportunities = calculate_all(matches, min_roi=settings.min_roi_pct)

                settled_count = await settle_ready_shadow_bets(client)
                if settled_count:
                    log.info("Settled %d verified paper positions", settled_count)

            # Publish only a complete, two-venue cycle.
            _latest_opportunities = opportunities
            _last_refresh = datetime.now(timezone.utc).isoformat()
            _last_error = None
            _scan_count += 1

            await asyncio.to_thread(log_opportunities, opportunities, scan_id)
            shadow_count = await asyncio.to_thread(process_shadow_bets, opportunities)
            await asyncio.to_thread(prune_history)
            await asyncio.to_thread(
                finish_scan,
                scan_id,
                status="COMPLETED",
                kalshi_markets=kalshi_count,
                polymarket_markets=poly_count,
                strict_matches=match_count,
                executable_opportunities=len(opportunities),
            )
            log.info(
                "Cycle %d: %d strict matches, %d executable, %d profitable, %d new paper",
                _scan_count,
                match_count,
                len(opportunities),
                sum(1 for opportunity in opportunities if opportunity.is_profitable),
                shadow_count,
            )
        except Exception as exc:
            _last_error = str(exc)
            await asyncio.to_thread(
                finish_scan,
                scan_id,
                status="FAILED",
                kalshi_markets=kalshi_count,
                polymarket_markets=poly_count,
                strict_matches=match_count,
                error=str(exc)[:1000],
            )
            raise


async def _background_refresh_loop() -> None:
    while True:
        try:
            await _refresh_data()
        except Exception as exc:
            log.error("Refresh cycle failed: %s", exc)
        await asyncio.sleep(settings.refresh_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_db)
    task = asyncio.create_task(_background_refresh_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Arbitrage Scanner",
    description="Executable Kalshi/Polymarket research scanner",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Arbitrage-Admin-Token"],
)


def _require_admin(request: Request, supplied_token: str | None) -> None:
    """Allow localhost controls; require a configured token from remote hosts."""
    host = request.client.host if request.client else ""
    if host in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return
    if settings.admin_token and supplied_token and hmac.compare_digest(
        supplied_token, settings.admin_token
    ):
        return
    raise HTTPException(status_code=403, detail="admin token required")


@app.get("/api/health")
async def health():
    now = datetime.now(timezone.utc)
    last = datetime.fromisoformat(_last_refresh) if _last_refresh else None
    age = (now - last).total_seconds() if last else None
    healthy = age is not None and age <= max(120, settings.refresh_interval * 3)
    return {
        "status": "ok" if healthy else "stale",
        "last_successful_refresh": _last_refresh,
        "age_seconds": round(age, 1) if age is not None else None,
        "last_error": _last_error,
        "scan_count": _scan_count,
        "data_version": DATA_VERSION,
    }


@app.get("/api/opportunities")
async def get_opportunities(
    min_roi: float = Query(0),
    min_confidence: float = Query(0),
    profitable_only: bool = Query(False),
):
    opportunities = _latest_opportunities
    if min_roi > 0:
        opportunities = [item for item in opportunities if item.roi_pct >= min_roi]
    if min_confidence > 0:
        opportunities = [item for item in opportunities if item.match_confidence >= min_confidence]
    if profitable_only:
        opportunities = [item for item in opportunities if item.is_profitable]
    return {
        "opportunities": [asdict(item) for item in opportunities],
        "count": len(opportunities),
        "last_refresh": _last_refresh,
        "scan_count": _scan_count,
        "refresh_interval": settings.refresh_interval,
        "data_version": DATA_VERSION,
    }


@app.get("/api/config")
async def get_config():
    return {
        "refresh_interval": settings.refresh_interval,
        "min_roi_pct": settings.min_roi_pct,
        "match_auto_threshold": settings.match_auto_threshold,
        "match_review_threshold": settings.match_review_threshold,
        "quote_size_contracts": settings.quote_size_contracts,
        "max_quote_age_seconds": settings.max_quote_age_seconds,
        "kalshi_fee_mode": settings.kalshi_fee_mode,
        "kalshi_taker_rate": KALSHI_TAKER_RATE,
        "kalshi_maker_rate": KALSHI_MAKER_RATE,
        "polymarket_taker_rates": POLYMARKET_TAKER_RATES,
        "data_version": DATA_VERSION,
    }


@app.post("/api/config")
async def update_config(
    request: Request,
    refresh_interval: Optional[int] = None,
    min_roi_pct: Optional[float] = None,
    kalshi_fee_mode: Optional[str] = None,
    x_arbitrage_admin_token: Optional[str] = Header(None),
):
    _require_admin(request, x_arbitrage_admin_token)
    if refresh_interval is not None and refresh_interval >= 5:
        settings.refresh_interval = refresh_interval
    if min_roi_pct is not None:
        settings.min_roi_pct = min_roi_pct
    if kalshi_fee_mode in {"taker", "maker"}:
        settings.kalshi_fee_mode = kalshi_fee_mode
    return {"status": "ok", "config": await get_config()}


@app.get("/api/stats")
async def get_stats():
    stats = await asyncio.to_thread(get_history_stats)
    stats["current_opportunities"] = len(_latest_opportunities)
    stats["current_profitable"] = sum(
        1 for opportunity in _latest_opportunities if opportunity.is_profitable
    )
    stats["scan_count"] = _scan_count
    stats["best_current_roi"] = max(
        (opportunity.roi_pct for opportunity in _latest_opportunities if opportunity.is_profitable),
        default=0,
    )
    return stats


@app.get("/api/shadow-bets")
async def get_shadow_bets():
    return await asyncio.to_thread(get_shadow_bets_summary)


@app.post("/api/refresh")
async def trigger_refresh(
    request: Request,
    x_arbitrage_admin_token: Optional[str] = Header(None),
):
    _require_admin(request, x_arbitrage_admin_token)
    await _refresh_data()
    return {"status": "ok", "scan_count": _scan_count}


@app.get("/api/fee-preview")
async def fee_preview(price: float = Query(0.5, ge=0, le=1), category: str = Query("other")):
    return {
        "price": price,
        "kalshi_taker_fee": kalshi_fee_per_contract(price),
        "kalshi_maker_fee": kalshi_fee_per_contract(price, maker=True),
        "polymarket_taker_fee": polymarket_fee_per_share(price, category),
        "polymarket_maker_fee": 0.0,
        "category": category,
    }


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@app.get("/")
async def index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
