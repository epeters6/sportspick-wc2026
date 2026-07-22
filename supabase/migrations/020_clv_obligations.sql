-- Migration 020: Durable CLV checkpoint obligations (minimal stub)
-- Tracks owed 15m / 1h / close price snapshots per candidate_id.
-- Compatible with the live schema (obs_* / book_ts_* columns).

CREATE TABLE IF NOT EXISTS clv_obligations (
    candidate_id     text PRIMARY KEY,
    platform         text NOT NULL DEFAULT 'unknown',
    market_id        text NOT NULL,
    outcome_id       text NOT NULL,
    side             text NOT NULL,
    entry_price      double precision NOT NULL,
    entry_ts         timestamptz NOT NULL,
    due_15m          timestamptz,
    due_1h           timestamptz,
    due_close        timestamptz,
    status_15m       text NOT NULL DEFAULT 'pending',
    status_1h        text NOT NULL DEFAULT 'pending',
    status_close     text NOT NULL DEFAULT 'pending',
    obs_15m_price    double precision,
    obs_1h_price     double precision,
    obs_close_price  double precision,
    obs_15m_ts       timestamptz,
    obs_1h_ts        timestamptz,
    obs_close_ts     timestamptz,
    book_ts_15m      timestamptz,
    book_ts_1h       timestamptz,
    book_ts_close    timestamptz,
    metadata         jsonb DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS clv_obligations_due_15m_idx
    ON clv_obligations (due_15m) WHERE status_15m = 'pending';
CREATE INDEX IF NOT EXISTS clv_obligations_due_1h_idx
    ON clv_obligations (due_1h) WHERE status_1h = 'pending';
CREATE INDEX IF NOT EXISTS clv_obligations_market_idx
    ON clv_obligations (market_id);

ALTER TABLE clv_obligations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "clv_obligations_read" ON clv_obligations;
CREATE POLICY "clv_obligations_read" ON clv_obligations
    FOR SELECT TO authenticated USING (true);
-- Mutations via service_role only (no anon/authenticated write policies)
