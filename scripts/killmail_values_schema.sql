CREATE SCHEMA IF NOT EXISTS market;

-- PostgreSQL adds this nullable column without rewriting existing partitions.
-- Integer cents retain zKill's two-decimal precision without NUMERIC overhead.
ALTER TABLE rawkm.killmails
ADD COLUMN IF NOT EXISTS estimated_value_cents BIGINT;

COMMENT ON COLUMN rawkm.killmails.estimated_value_cents IS
 'Estimated total killmail value in hundredths of ISK, calculated from the local zKill daily price history.';

ALTER TABLE rawkm.killmails ADD COLUMN IF NOT EXISTS live_price_date date;
