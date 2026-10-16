CREATE SCHEMA IF NOT EXISTS market;
-- Sparse supplementary quotes only. Original daily snapshots and hashes are preserved.
CREATE TABLE IF NOT EXISTS market.zkill_recovered_prices (
 price_date date NOT NULL,
 item_type_id bigint NOT NULL,
 unit_price_isk numeric NOT NULL CHECK (unit_price_isk > 0),
 source_killmail_id bigint NOT NULL,
 source_url text NOT NULL,
 fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (price_date,item_type_id)
);
CREATE TABLE IF NOT EXISTS market.killmail_price_gaps (
 price_date date NOT NULL,
 item_type_id bigint NOT NULL,
 item_name text NOT NULL DEFAULT '',
 reason text NOT NULL,
 detail text NOT NULL,
 status text NOT NULL DEFAULT 'pending',
 active boolean NOT NULL DEFAULT true,
 candidate_killmail_ids bigint[] NOT NULL DEFAULT '{}',
 source_killmail_id bigint,
 unit_price_isk numeric,
 last_error text,
 checked_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (price_date,item_type_id)
);

-- Item-page prices and explicitly requested fallbacks have no source killmail.
ALTER TABLE market.zkill_recovered_prices ALTER COLUMN source_killmail_id DROP NOT NULL;
ALTER TABLE market.zkill_recovered_prices ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'kill_page';
ALTER TABLE market.zkill_recovered_prices ADD COLUMN IF NOT EXISTS source_price_date date;
ALTER TABLE market.killmail_price_gaps ADD COLUMN IF NOT EXISTS source_kind text;
ALTER TABLE market.killmail_price_gaps ADD COLUMN IF NOT EXISTS source_price_date date;
