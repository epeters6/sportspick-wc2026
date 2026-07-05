-- Migration: Create weather_verification table for MOS bias correction
-- Purpose: Log historical forecasts vs actuals to train station-level bias models.

CREATE TABLE IF NOT EXISTS weather_verification (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    station_id text NOT NULL, -- e.g., KNYC
    lead_time_days int NOT NULL,
    model_name text NOT NULL, -- e.g., ecmwf_ifs04
    predicted_high float8,
    predicted_low float8,
    actual_high float8,
    actual_low float8,
    target_date date NOT NULL,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    UNIQUE(station_id, lead_time_days, model_name, target_date)
);

-- Enable RLS
ALTER TABLE weather_verification ENABLE ROW LEVEL SECURITY;

-- Allow read/write for anon key (like other tables in this local db)
CREATE POLICY "Enable read access for all users" ON weather_verification
    FOR SELECT USING (true);
CREATE POLICY "Enable insert for all users" ON weather_verification
    FOR INSERT WITH CHECK (true);
CREATE POLICY "Enable update for all users" ON weather_verification
    FOR UPDATE USING (true);
