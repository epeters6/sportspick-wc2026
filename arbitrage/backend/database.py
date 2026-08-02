"""Versioned SQLite history and honest paper-trading accounting."""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .config import settings
from .models import ArbitrageOpportunity

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "arbitrage_history.db")
DATA_VERSION = "executable_orderbook_v3"
LEGACY_VERSION = "legacy_reference_v1"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def init_db() -> None:
    """Create the current schema and quarantine legacy, non-executable paper rows."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_cycles (
            scan_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            kalshi_markets INTEGER DEFAULT 0,
            polymarket_markets INTEGER DEFAULT 0,
            strict_matches INTEGER DEFAULT 0,
            executable_opportunities INTEGER DEFAULT 0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_name TEXT NOT NULL,
            kalshi_title TEXT,
            polymarket_title TEXT,
            kalshi_url TEXT,
            polymarket_url TEXT,
            match_confidence REAL,
            kalshi_yes REAL,
            kalshi_no REAL,
            polymarket_yes REAL,
            polymarket_no REAL,
            buy_yes_on TEXT,
            buy_no_on TEXT,
            kalshi_fee REAL,
            polymarket_fee REAL,
            total_fees REAL,
            gross_gap REAL,
            net_gap REAL,
            capital_required REAL,
            roi_pct REAL,
            kalshi_volume REAL,
            polymarket_volume REAL,
            category TEXT,
            is_profitable INTEGER
        );

        CREATE TABLE IF NOT EXISTS shadow_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_name TEXT NOT NULL,
            kalshi_title TEXT,
            polymarket_title TEXT,
            match_confidence REAL,
            buy_yes_on TEXT,
            buy_no_on TEXT,
            size_contracts REAL DEFAULT 0,
            entry_kalshi_price REAL,
            entry_poly_price REAL,
            entry_capital REAL,
            expected_net_gap REAL,
            expected_roi REAL,
            status TEXT DEFAULT 'OPEN',
            simulated_pnl REAL DEFAULT 0
        );
    """)

    _ensure_columns(conn, "opportunities", {
        "scan_id": "TEXT",
        "kalshi_market_id": "TEXT",
        "polymarket_market_id": "TEXT",
        "inverted_outcomes": "INTEGER DEFAULT 0",
        "kalshi_buy_side": "TEXT",
        "polymarket_buy_side": "TEXT",
        "kalshi_leg_price": "REAL",
        "polymarket_leg_price": "REAL",
        "executable_size": "REAL DEFAULT 0",
        "execution_buffer": "REAL DEFAULT 0",
        "quote_source": "TEXT",
        "kalshi_quote_received_at": "TEXT",
        "polymarket_quote_received_at": "TEXT",
        "quote_valid": "INTEGER DEFAULT 0",
        "suspicious": "INTEGER DEFAULT 1",
        "data_version": f"TEXT NOT NULL DEFAULT '{LEGACY_VERSION}'",
    })
    _ensure_columns(conn, "shadow_bets", {
        "pair_key": "TEXT",
        "kalshi_market_id": "TEXT",
        "polymarket_market_id": "TEXT",
        "inverted_outcomes": "INTEGER DEFAULT 0",
        "kalshi_buy_side": "TEXT",
        "polymarket_buy_side": "TEXT",
        "expected_pnl": "REAL DEFAULT 0",
        "realized_pnl": "REAL",
        "settled_at": "TEXT",
        "kalshi_result": "TEXT",
        "polymarket_result": "TEXT",
        "settlement_error": "TEXT",
        "data_version": f"TEXT NOT NULL DEFAULT '{LEGACY_VERSION}'",
    })

    # Old rows were generated from reference prices and cannot be settled
    # safely because venue IDs and exact bought sides were never stored.
    conn.execute(
        """UPDATE shadow_bets SET status='QUARANTINED'
           WHERE status='OPEN' AND data_version=?""",
        (LEGACY_VERSION,),
    )
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_opp_timestamp ON opportunities(timestamp);
        CREATE INDEX IF NOT EXISTS idx_opp_profitable ON opportunities(is_profitable, roi_pct);
        CREATE INDEX IF NOT EXISTS idx_opp_version_scan ON opportunities(data_version, scan_id);
        CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_bets(status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_open_pair
            ON shadow_bets(pair_key) WHERE status='OPEN' AND pair_key IS NOT NULL;
    """)
    conn.commit()
    conn.close()


def start_scan() -> str:
    scan_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        "INSERT INTO scan_cycles(scan_id, started_at, status) VALUES (?,?,?)",
        (scan_id, datetime.now(timezone.utc).isoformat(), "RUNNING"),
    )
    conn.commit()
    conn.close()
    return scan_id


def finish_scan(
    scan_id: str,
    *,
    status: str,
    kalshi_markets: int = 0,
    polymarket_markets: int = 0,
    strict_matches: int = 0,
    executable_opportunities: int = 0,
    error: str | None = None,
) -> None:
    conn = _get_conn()
    conn.execute(
        """UPDATE scan_cycles SET completed_at=?, status=?, kalshi_markets=?,
           polymarket_markets=?, strict_matches=?, executable_opportunities=?, error=?
           WHERE scan_id=?""",
        (
            datetime.now(timezone.utc).isoformat(), status, kalshi_markets,
            polymarket_markets, strict_matches, executable_opportunities,
            error, scan_id,
        ),
    )
    conn.commit()
    conn.close()


def log_opportunities(opps: List[ArbitrageOpportunity], scan_id: str) -> None:
    if not opps:
        return
    rows = [(
        o.timestamp, scan_id, o.event_name, o.kalshi_title, o.polymarket_title,
        o.kalshi_url, o.polymarket_url, o.match_confidence,
        o.kalshi_market_id, o.polymarket_market_id, int(o.inverted_outcomes),
        o.kalshi_yes, o.kalshi_no, o.polymarket_yes, o.polymarket_no,
        o.buy_yes_on, o.buy_no_on, o.kalshi_buy_side, o.polymarket_buy_side,
        o.kalshi_leg_price, o.polymarket_leg_price,
        o.kalshi_fee, o.polymarket_fee, o.total_fees, o.execution_buffer,
        o.gross_gap, o.net_gap, o.capital_required, o.roi_pct,
        o.executable_size, o.kalshi_volume, o.polymarket_volume, o.category,
        int(o.is_profitable), o.quote_source, o.kalshi_quote_received_at,
        o.polymarket_quote_received_at, int(o.quote_valid), int(o.suspicious),
        DATA_VERSION,
    ) for o in opps]
    conn = _get_conn()
    conn.executemany("""
        INSERT INTO opportunities (
            timestamp, scan_id, event_name, kalshi_title, polymarket_title,
            kalshi_url, polymarket_url, match_confidence,
            kalshi_market_id, polymarket_market_id, inverted_outcomes,
            kalshi_yes, kalshi_no, polymarket_yes, polymarket_no,
            buy_yes_on, buy_no_on, kalshi_buy_side, polymarket_buy_side,
            kalshi_leg_price, polymarket_leg_price,
            kalshi_fee, polymarket_fee, total_fees, execution_buffer,
            gross_gap, net_gap, capital_required, roi_pct, executable_size,
            kalshi_volume, polymarket_volume, category, is_profitable,
            quote_source, kalshi_quote_received_at, polymarket_quote_received_at,
            quote_valid, suspicious, data_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    conn.close()


def prune_history() -> int:
    """Bound only current verified history; legacy evidence remains quarantined."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.history_retention_days)).isoformat()
    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM opportunities WHERE data_version=? AND timestamp < ?",
        (DATA_VERSION, cutoff),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def _pair_key(opp: ArbitrageOpportunity) -> str:
    return ":".join((
        opp.kalshi_market_id,
        opp.polymarket_market_id,
        opp.kalshi_buy_side,
        opp.polymarket_buy_side,
    ))


def log_shadow_bet(opp: ArbitrageOpportunity, size_contracts: int = 25) -> Optional[int]:
    """Reserve capital for an executable paper position; do not credit P&L."""
    if not opp.is_profitable or opp.executable_size < settings.min_executable_contracts:
        return None
    conn = _get_conn()
    key = _pair_key(opp)
    if conn.execute(
        "SELECT 1 FROM shadow_bets WHERE pair_key=? AND status='OPEN'", (key,)
    ).fetchone():
        conn.close()
        return None

    open_capital = conn.execute(
        "SELECT COALESCE(SUM(entry_capital),0) FROM shadow_bets WHERE status='OPEN' AND data_version=?",
        (DATA_VERSION,),
    ).fetchone()[0]
    available_capital = max(0.0, settings.shadow_bankroll - float(open_capital or 0.0))
    size = min(float(size_contracts), float(opp.executable_size))
    if opp.capital_required <= 0:
        conn.close()
        return None
    size = min(size, available_capital / opp.capital_required)
    if size < settings.min_executable_contracts:
        conn.close()
        return None

    entry_capital = opp.capital_required * size
    expected_pnl = opp.net_gap * size
    cursor = conn.execute("""
        INSERT INTO shadow_bets (
            timestamp, event_name, kalshi_title, polymarket_title,
            match_confidence, buy_yes_on, buy_no_on, size_contracts,
            entry_kalshi_price, entry_poly_price, entry_capital,
            expected_net_gap, expected_roi, status, simulated_pnl,
            pair_key, kalshi_market_id, polymarket_market_id, inverted_outcomes,
            kalshi_buy_side, polymarket_buy_side, expected_pnl, realized_pnl,
            data_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',0,?,?,?,?,?,?,?,NULL,?)
    """, (
        opp.timestamp, opp.event_name, opp.kalshi_title, opp.polymarket_title,
        opp.match_confidence, opp.buy_yes_on, opp.buy_no_on, size,
        opp.kalshi_leg_price, opp.polymarket_leg_price, entry_capital,
        opp.net_gap, opp.roi_pct, key, opp.kalshi_market_id,
        opp.polymarket_market_id, int(opp.inverted_outcomes),
        opp.kalshi_buy_side, opp.polymarket_buy_side, expected_pnl, DATA_VERSION,
    ))
    bet_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def get_open_shadow_bets() -> list[dict]:
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM shadow_bets WHERE status='OPEN' AND data_version=?",
        (DATA_VERSION,),
    )]
    conn.close()
    return rows


def settle_shadow_bet(bet_id: int, kalshi_result: str, polymarket_result: str) -> bool:
    """Settle only when both venue results agree with the stored alignment."""
    kalshi_result = kalshi_result.lower()
    polymarket_result = polymarket_result.lower()
    if kalshi_result not in {"yes", "no"} or polymarket_result not in {"yes", "no"}:
        return False
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM shadow_bets WHERE id=? AND status='OPEN'", (bet_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return False

    aligned = kalshi_result != polymarket_result if row["inverted_outcomes"] else kalshi_result == polymarket_result
    if not aligned:
        conn.execute(
            "UPDATE shadow_bets SET status='REVIEW', settlement_error=? WHERE id=?",
            ("venue results disagree with stored outcome alignment", bet_id),
        )
        conn.commit()
        conn.close()
        return False

    size = float(row["size_contracts"] or 0)
    payout = size * (
        int(row["kalshi_buy_side"] == kalshi_result)
        + int(row["polymarket_buy_side"] == polymarket_result)
    )
    realized = payout - float(row["entry_capital"] or 0)
    conn.execute("""
        UPDATE shadow_bets SET status='SETTLED', realized_pnl=?, settled_at=?,
        kalshi_result=?, polymarket_result=?, settlement_error=NULL WHERE id=?
    """, (
        realized, datetime.now(timezone.utc).isoformat(),
        kalshi_result, polymarket_result, bet_id,
    ))
    conn.commit()
    conn.close()
    return True


def get_shadow_bets_summary() -> dict:
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    recent_rows = [dict(row) for row in conn.execute(
        "SELECT * FROM shadow_bets ORDER BY id DESC LIMIT 50"
    )]
    verified = conn.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='OPEN' THEN entry_capital ELSE 0 END),
               SUM(CASE WHEN status='OPEN' THEN expected_pnl ELSE 0 END),
               SUM(CASE WHEN status='SETTLED' THEN realized_pnl ELSE 0 END)
        FROM shadow_bets WHERE data_version=?
    """, (DATA_VERSION,)).fetchone()
    quarantined = conn.execute(
        "SELECT COUNT(*) FROM shadow_bets WHERE status='QUARANTINED'"
    ).fetchone()[0]
    conn.close()

    total_bets = verified[0] or 0
    open_bets = verified[1] or 0
    open_capital = round(verified[2] or 0.0, 2)
    expected_open_pnl = round(verified[3] or 0.0, 2)
    realized_pnl = round(verified[4] or 0.0, 2)
    return {
        "starting_balance": settings.shadow_bankroll,
        "current_balance": round(settings.shadow_bankroll + realized_pnl, 2),
        "realized_pnl": realized_pnl,
        "total_paper_pnl": realized_pnl,
        "expected_open_pnl": expected_open_pnl,
        "total_paper_bets": total_bets,
        "open_paper_bets": open_bets,
        "open_capital": open_capital,
        "total_capital_deployed": open_capital,
        "quarantined_legacy_bets": quarantined,
        "recent_bets": recent_rows,
    }


def get_history_stats() -> dict:
    conn = _get_conn()
    valid = conn.execute("""
        SELECT COUNT(*), SUM(is_profitable), MAX(CASE WHEN is_profitable=1 THEN roi_pct END),
               AVG(CASE WHEN is_profitable=1 THEN roi_pct END), MIN(timestamp), MAX(timestamp)
        FROM opportunities WHERE data_version=?
    """, (DATA_VERSION,)).fetchone()
    cycles = conn.execute(
        "SELECT COUNT(*) FROM scan_cycles WHERE status='COMPLETED'"
    ).fetchone()[0]
    failed_cycles = conn.execute(
        "SELECT COUNT(*) FROM scan_cycles WHERE status='FAILED'"
    ).fetchone()[0]
    legacy = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE data_version<>?", (DATA_VERSION,)
    ).fetchone()[0]
    conn.close()
    return {
        "total_records": valid[0] or 0,
        "total_cycles": cycles,
        "failed_cycles": failed_cycles,
        "profitable_records": valid[1] or 0,
        "best_roi_ever": valid[2] or 0,
        "avg_profitable_roi": round(valid[3] or 0, 2),
        "first_scan": valid[4] or "",
        "last_scan": valid[5] or "",
        "legacy_unverified_records": legacy,
        "data_version": DATA_VERSION,
    }
