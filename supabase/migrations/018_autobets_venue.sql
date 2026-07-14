-- Migration 018: autobets.venue for Kalshi vs Polymarket routing
ALTER TABLE autobets ADD COLUMN IF NOT EXISTS venue text;
CREATE INDEX IF NOT EXISTS idx_autobets_venue ON autobets (venue);
