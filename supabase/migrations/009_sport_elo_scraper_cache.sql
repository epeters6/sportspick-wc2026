-- Per-sport Elo / CLV and scraper negative cache (e.g. ActionNetwork 404s)

ALTER TABLE influencers ADD COLUMN IF NOT EXISTS elo_by_sport jsonb DEFAULT '{}';
ALTER TABLE influencers ADD COLUMN IF NOT EXISTS avg_clv_by_sport jsonb DEFAULT '{}';

CREATE TABLE IF NOT EXISTS scraper_cache (
    cache_key   text PRIMARY KEY,
    cache_value jsonb NOT NULL DEFAULT '{}',
    expires_at  timestamptz,
    created_at  timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scraper_cache_expires_idx ON scraper_cache (expires_at);

ALTER TABLE scraper_cache ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_scraper_cache" ON scraper_cache;
CREATE POLICY "service_role_all_scraper_cache"
    ON scraper_cache FOR ALL USING (auth.role() = 'service_role');
