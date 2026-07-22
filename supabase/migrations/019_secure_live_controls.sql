-- Migration 019: harden live-trading control plane
-- Writes to app_settings / weather_verification go through service_role only.
-- Dashboard may still SELECT toggle state; mutations are not open to anon keys.

-- ─── app_settings: drop open write policies ─────────────────────────────────
DROP POLICY IF EXISTS "app_settings_insert" ON app_settings;
DROP POLICY IF EXISTS "app_settings_update" ON app_settings;
DROP POLICY IF EXISTS "app_settings_delete" ON app_settings;

-- Keep public SELECT so the dashboard can read toggle state without a write key.
DROP POLICY IF EXISTS "app_settings_read" ON app_settings;
CREATE POLICY "app_settings_read" ON app_settings
    FOR SELECT USING (true);

-- ─── weather_verification: drop open write policies ─────────────────────────
DROP POLICY IF EXISTS "Enable insert for all users" ON weather_verification;
DROP POLICY IF EXISTS "Enable update for all users" ON weather_verification;
DROP POLICY IF EXISTS "Enable delete for all users" ON weather_verification;

-- Keep SELECT for dashboard / MOS reads; mutations via service_role only (RLS bypass).
DROP POLICY IF EXISTS "Enable read access for all users" ON weather_verification;
DROP POLICY IF EXISTS "weather_verification_read" ON weather_verification;
CREATE POLICY "weather_verification_read" ON weather_verification
    FOR SELECT USING (true);

-- ─── rls_auto_enable: deny client roles if the helper exists ────────────────
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable'
  ) THEN
    EXECUTE 'REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM PUBLIC';
    BEGIN
      EXECUTE 'REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM anon';
    EXCEPTION WHEN undefined_object THEN NULL;
    END;
    BEGIN
      EXECUTE 'REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM authenticated';
    EXCEPTION WHEN undefined_object THEN NULL;
    END;
  END IF;
END $$;

-- ─── live_toggle_audit: append-only audit trail for enable/disable attempts ─
CREATE TABLE IF NOT EXISTS live_toggle_audit (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    previous_value  jsonb,
    requested_value jsonb,
    actor           text,
    created_at      timestamptz DEFAULT now(),
    readiness       jsonb,
    reason          text,
    allowed         boolean
);

ALTER TABLE live_toggle_audit ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "live_toggle_audit_read" ON live_toggle_audit;
CREATE POLICY "live_toggle_audit_read" ON live_toggle_audit
    FOR SELECT TO authenticated USING (true);

-- No INSERT/UPDATE/DELETE policies for anon/authenticated —
-- only service_role (RLS bypass) may write audit rows.
