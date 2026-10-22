-- Frozen 30m V3 has no profit target. Preserve that as NULL, not a fake price.
-- Existing strategies retain their actual numeric targets.
SET lock_timeout = '5s';
ALTER TABLE strategy_trades ALTER COLUMN target DROP NOT NULL;
