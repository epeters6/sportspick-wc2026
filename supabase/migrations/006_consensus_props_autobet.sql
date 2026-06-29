-- Prop consensus + autobet bet metadata
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS bet_type text DEFAULT 'moneyline';
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS bet_line text;
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS consensus_key text;

ALTER TABLE autobets ADD COLUMN IF NOT EXISTS bet_type text DEFAULT 'moneyline';
ALTER TABLE autobets ADD COLUMN IF NOT EXISTS bet_line text;

-- Backfill consensus_key for existing rows
UPDATE consensus_picks
SET consensus_key = COALESCE(bet_type, 'moneyline') || '|' || predicted_winner || '|' || COALESCE(bet_line, '')
WHERE consensus_key IS NULL;

ALTER TABLE consensus_picks DROP CONSTRAINT IF EXISTS consensus_picks_match_id_predicted_winner_key;

CREATE UNIQUE INDEX IF NOT EXISTS consensus_picks_match_key_idx
    ON consensus_picks (match_id, consensus_key);
