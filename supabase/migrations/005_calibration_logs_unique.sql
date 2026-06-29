-- Fix calibration_logs upsert (PostgREST needs a non-partial unique constraint)
ALTER TABLE calibration_logs
    DROP CONSTRAINT IF EXISTS calibration_logs_match_outcome_unique;

ALTER TABLE calibration_logs
    ADD CONSTRAINT calibration_logs_match_outcome_unique
    UNIQUE (match_id, predicted_outcome);
