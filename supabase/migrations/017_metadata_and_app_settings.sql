-- Migration 017: autobets metadata + app_settings (live trading toggle)

-- Rich per-bet context (weather bucket bounds, station, metric, MOS bias...)
-- used by settlement grading and the dashboard.
ALTER TABLE autobets ADD COLUMN IF NOT EXISTS metadata jsonb DEFAULT '{}'::jsonb;

-- Simple key-value app settings, used for the dashboard live-trading toggle.
CREATE TABLE IF NOT EXISTS app_settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz DEFAULT now()
);

ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "app_settings_read" ON app_settings;
CREATE POLICY "app_settings_read" ON app_settings FOR SELECT USING (true);
DROP POLICY IF EXISTS "app_settings_insert" ON app_settings;
CREATE POLICY "app_settings_insert" ON app_settings FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS "app_settings_update" ON app_settings;
CREATE POLICY "app_settings_update" ON app_settings FOR UPDATE USING (true);

-- Seed the live trading toggle OFF
INSERT INTO app_settings (key, value)
VALUES ('live_trading', '{"enabled": false, "enabled_by": null, "enabled_at": null}'::jsonb)
ON CONFLICT (key) DO NOTHING;
