-- ============================================================
-- Migration 002: ML improvements, new bet types, MLB, calibration
-- Applied: 2026-06
-- ============================================================

-- ── picks: bet type columns ─────────────────────────────────
ALTER TABLE picks ADD COLUMN IF NOT EXISTS bet_type text DEFAULT 'moneyline';
ALTER TABLE picks ADD COLUMN IF NOT EXISTS bet_line text;

-- ── consensus_picks: full probability distribution ──────────
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS home_probability float DEFAULT 0.0;
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS draw_probability float DEFAULT 0.0;
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS away_probability float DEFAULT 0.0;
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS pick_count int DEFAULT 0;
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS alerted_at timestamptz;

-- ── sport_type enum: add MLB ─────────────────────────────────
ALTER TYPE sport_type ADD VALUE IF NOT EXISTS 'mlb';

-- ── calibration_logs ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calibration_logs (
    id                  uuid primary key default uuid_generate_v4(),
    match_id            uuid references matches(id) on delete cascade,
    bet_type            text default 'moneyline',
    predicted_outcome   text,
    confidence          float,
    actual_outcome      text,
    brier_contribution  float,
    is_correct          boolean,
    logged_at           timestamptz default now()
);

CREATE UNIQUE INDEX IF NOT EXISTS calibration_match_outcome_idx
    ON calibration_logs(match_id, predicted_outcome)
    WHERE match_id IS NOT NULL;

ALTER TABLE calibration_logs ENABLE ROW LEVEL SECURITY;

-- ── simulated_bets (paper trading) ────────────────────────────
CREATE TABLE IF NOT EXISTS simulated_bets (
    id                  uuid primary key default uuid_generate_v4(),
    match_id            uuid references matches(id) on delete cascade,
    predicted_outcome   text,
    bet_type            text default 'moneyline',
    bet_line            text,
    confidence          float,
    edge                float,
    kelly_fraction      float,
    bet_size            float,
    bankroll_at_time    float,
    outcome             text,
    pnl                 float,
    created_at          timestamptz default now(),
    resolved_at         timestamptz
);

CREATE INDEX IF NOT EXISTS simbet_match_idx   ON simulated_bets(match_id);
CREATE INDEX IF NOT EXISTS simbet_created_idx ON simulated_bets(created_at DESC);

ALTER TABLE simulated_bets ENABLE ROW LEVEL SECURITY;
