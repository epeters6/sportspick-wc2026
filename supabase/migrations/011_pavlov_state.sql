-- Pavlov bot persistent state (replaces Railway volume JSON files)

CREATE TABLE IF NOT EXISTS pavlov_state (
    namespace   text NOT NULL,
    file_path   text NOT NULL,
    content     jsonb NOT NULL DEFAULT '{}',
    updated_at  timestamptz DEFAULT now(),
    PRIMARY KEY (namespace, file_path)
);

CREATE INDEX IF NOT EXISTS pavlov_state_namespace_idx ON pavlov_state (namespace);

ALTER TABLE pavlov_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_pavlov_state" ON pavlov_state;
CREATE POLICY "service_role_all_pavlov_state"
    ON pavlov_state FOR ALL USING (auth.role() = 'service_role');
