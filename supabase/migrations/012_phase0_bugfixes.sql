ALTER TABLE influencers
ADD COLUMN IF NOT EXISTS wilson_score float default 0.0;
