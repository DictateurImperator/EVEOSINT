CREATE SCHEMA IF NOT EXISTS market;
-- Daily JSONB snapshots use PostgreSQL TOAST compression. No large per-item GIN index.
CREATE TABLE IF NOT EXISTS market.zkill_daily_prices (
 price_date DATE PRIMARY KEY,
 prices JSONB NOT NULL CHECK (jsonb_typeof(prices) = 'object'),
 item_count INTEGER NOT NULL CHECK (item_count >= 0),
 status TEXT NOT NULL CHECK (status IN ('imported', 'empty')),
 source_url TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK ((status = 'empty' AND item_count = 0) OR (status = 'imported' AND item_count > 0))
);
COMMENT ON TABLE market.zkill_daily_prices IS
 'zKill daily valuation snapshots, not guaranteed historical market transactions. Missing item keys mean unknown, never zero.';
