ALTER TABLE autobets 
ADD COLUMN IF NOT EXISTS closing_price float8,
ADD COLUMN IF NOT EXISTS clv float8;

ALTER TABLE simulated_bets 
ADD COLUMN IF NOT EXISTS closing_price float8,
ADD COLUMN IF NOT EXISTS clv float8;
