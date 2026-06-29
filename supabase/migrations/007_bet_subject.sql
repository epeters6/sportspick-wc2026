-- Who/what an O/U or prop applies to (player name, team, "match", "1h", etc.)
ALTER TABLE picks ADD COLUMN IF NOT EXISTS bet_subject text;
ALTER TABLE consensus_picks ADD COLUMN IF NOT EXISTS bet_subject text;
ALTER TABLE autobets ADD COLUMN IF NOT EXISTS bet_subject text;
ALTER TABLE simulated_bets ADD COLUMN IF NOT EXISTS bet_subject text;

UPDATE consensus_picks
SET consensus_key = COALESCE(bet_type, 'moneyline') || '|' || predicted_winner || '|'
    || COALESCE(bet_line, '') || '|' || COALESCE(bet_subject, '')
WHERE consensus_key IS NOT NULL;
