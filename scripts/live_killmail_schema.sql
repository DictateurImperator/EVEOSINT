CREATE TABLE IF NOT EXISTS rawkm.live_killmail_cursor (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    next_sequence bigint NOT NULL,
    last_killmail_id bigint,
    processed bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE SCHEMA IF NOT EXISTS market;
CREATE TABLE IF NOT EXISTS market.live_valuation_claims (
    month date PRIMARY KEY,
    owner_pid integer NOT NULL,
    imported_through timestamptz NOT NULL
);
ALTER TABLE rawkm.killmails ADD COLUMN IF NOT EXISTS live_price_date date;
COMMENT ON COLUMN rawkm.killmails.live_price_date IS
    'Live estimate awaiting the daily loss-date price reconciliation; cleared by the standard calculator.';
