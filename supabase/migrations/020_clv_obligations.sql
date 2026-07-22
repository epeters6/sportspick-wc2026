-- Migration 020: durable CLV obligations across GitHub runners
CREATE TABLE IF NOT EXISTS clv_obligations (
    candidate_id text PRIMARY KEY,
    platform text,
    market_id text NOT NULL,
    outcome_id text NOT NULL,
    side text NOT NULL,
    entry_price float8 NOT NULL,
    entry_ts timestamptz NOT NULL,
    due_15m timestamptz,
    due_1h timestamptz,
    due_close timestamptz,
    status_15m text NOT NULL DEFAULT 'pending',
    status_1h text NOT NULL DEFAULT 'pending',
    status_close text NOT NULL DEFAULT 'pending',
    obs_15m_price float8,
    obs_1h_price float8,
    obs_close_price float8,
    obs_15m_ts timestamptz,
    obs_1h_ts timestamptz,
    obs_close_ts timestamptz,
    book_ts_15m timestamptz,
    book_ts_1h timestamptz,
    book_ts_close timestamptz,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

ALTER TABLE clv_obligations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "clv_obligations_read" ON clv_obligations;
CREATE POLICY "clv_obligations_read" ON clv_obligations
    FOR SELECT TO authenticated USING (true);
-- Writes via service_role only
