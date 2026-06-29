-- Autobet learning fields (optional — code works without them via fallbacks)
ALTER TABLE autobets ADD COLUMN IF NOT EXISTS raw_confidence float;
ALTER TABLE autobets ADD COLUMN IF NOT EXISTS sport text;
