-- Migration 021: Separate market entry price from effective cost on CLV obligations.
-- entry_price remains as the market fill price (backward compatible).
-- entry_effective_cost stores limit/executable cost for execution-adjusted CLV.

ALTER TABLE clv_obligations
    ADD COLUMN IF NOT EXISTS entry_market_price double precision,
    ADD COLUMN IF NOT EXISTS entry_effective_cost double precision;

-- Backfill from legacy entry_price when new columns are null
UPDATE clv_obligations
SET entry_market_price = COALESCE(entry_market_price, entry_price)
WHERE entry_market_price IS NULL;

UPDATE clv_obligations
SET entry_effective_cost = COALESCE(entry_effective_cost, entry_price)
WHERE entry_effective_cost IS NULL;
