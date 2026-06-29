-- ============================================================
-- Migration 003: Polymarket autobet + CLV-based Elo
-- Applied: 2026-06
-- ============================================================

-- ── picks: market line snapshot for Closing Line Value ──────
ALTER TABLE picks ADD COLUMN IF NOT EXISTS market_prob_at_pick float;

-- ── influencers: average CLV (best long-run skill predictor) ─
ALTER TABLE influencers ADD COLUMN IF NOT EXISTS avg_clv float;

-- ── autobets: Polymarket value-bet ledger ────────────────────
-- mode = 'paper' (records vs real prices) | 'live' (real CLOB orders)
-- status = 'open' | 'won' | 'lost' | 'void' | 'rejected'
CREATE TABLE IF NOT EXISTS autobets (
    id                uuid primary key default uuid_generate_v4(),
    match_id          uuid references matches(id) on delete cascade,
    market_id         text,           -- Polymarket conditionId
    market_slug       text,
    question          text,
    outcome_name      text,           -- canonical team / 'draw' we backed
    token_id          text,           -- CLOB token id of the bet outcome
    mode              text default 'paper',
    model_prob        float,          -- our calibrated + blended probability
    market_prob       float,          -- vig-free market implied probability
    market_price      float,          -- price a taker paid (ask/mid)
    edge              float,          -- model_prob - market_price - fees
    kelly_fraction    float,          -- raw (pre-cap) fractional Kelly
    stake             float,          -- USDC staked
    bankroll_at_time  float,
    shares            float,          -- stake / price
    status            text default 'open',
    pnl               float,
    clob_order_id     text,           -- live order id (live mode only)
    reject_reason     text,           -- why a candidate was rejected
    created_at        timestamptz default now(),
    resolved_at       timestamptz
);

CREATE INDEX IF NOT EXISTS autobets_match_idx   ON autobets(match_id);
CREATE INDEX IF NOT EXISTS autobets_status_idx  ON autobets(status);
CREATE INDEX IF NOT EXISTS autobets_created_idx ON autobets(created_at DESC);

-- Only one OPEN bet per (market, outcome, mode); rejected/settled rows are free
CREATE UNIQUE INDEX IF NOT EXISTS autobets_market_outcome_open_idx
    ON autobets(market_id, outcome_name, mode)
    WHERE status = 'open';

ALTER TABLE autobets ENABLE ROW LEVEL SECURITY;
