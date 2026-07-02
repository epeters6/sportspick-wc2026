-- Scraper platforms used by Covers, ActionNetwork, Pickswise
ALTER TYPE platform_type ADD VALUE IF NOT EXISTS 'covers';
ALTER TYPE platform_type ADD VALUE IF NOT EXISTS 'actionnetwork';
ALTER TYPE platform_type ADD VALUE IF NOT EXISTS 'pickswise';
