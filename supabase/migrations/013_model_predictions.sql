CREATE TABLE IF NOT EXISTS model_predictions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL, -- 'consensus', 'weather_model', 'sports_ml', 'manual'
    domain TEXT NOT NULL, -- 'sports', 'weather', 'macro'
    event_key TEXT NOT NULL, -- Stable ID e.g. match_id or 'NYC-high-temp-2026-07-03'
    outcome TEXT NOT NULL, -- What is predicted
    prob FLOAT NOT NULL, -- Probability
    market_price FLOAT,
    edge FLOAT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE,
    is_correct BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_model_predictions_event_key ON model_predictions(event_key);
CREATE INDEX IF NOT EXISTS idx_model_predictions_source ON model_predictions(source);

ALTER TABLE model_predictions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_model_predictions" ON model_predictions;
CREATE POLICY "service_role_all_model_predictions"
    ON model_predictions FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "public_read_model_predictions" ON model_predictions;
CREATE POLICY "public_read_model_predictions"
    ON model_predictions FOR SELECT USING (true);
