-- Box score / match statistics for prop settlement
ALTER TABLE matches ADD COLUMN IF NOT EXISTS match_stats jsonb;
ALTER TABLE matches ADD COLUMN IF NOT EXISTS stats_fetched_at timestamptz;

CREATE INDEX IF NOT EXISTS matches_stats_fetched_idx ON matches (stats_fetched_at)
    WHERE match_stats IS NOT NULL;
