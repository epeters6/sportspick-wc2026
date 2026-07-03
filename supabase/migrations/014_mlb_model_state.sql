CREATE TABLE IF NOT EXISTS mlb_model_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state_key TEXT NOT NULL UNIQUE,
    state_value JSONB DEFAULT '{}'::jsonb,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mlb_model_state_key ON mlb_model_state(state_key);

ALTER TABLE mlb_model_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_mlb_model_state" ON mlb_model_state;
CREATE POLICY "service_role_all_mlb_model_state"
    ON mlb_model_state FOR ALL USING (auth.role() = 'service_role');
