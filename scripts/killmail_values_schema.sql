CREATE SCHEMA IF NOT EXISTS market;

-- PostgreSQL adds this nullable column without rewriting existing partitions.
-- Integer cents retain zKill's two-decimal precision without NUMERIC overhead.
ALTER TABLE rawkm.killmails
ADD COLUMN IF NOT EXISTS estimated_value_cents BIGINT;

COMMENT ON COLUMN rawkm.killmails.estimated_value_cents IS
 'Estimated total killmail value in hundredths of ISK, calculated from the local zKill daily price history.';

CREATE TABLE IF NOT EXISTS market.killmail_valuation_months (
    month_start DATE PRIMARY KEY,
    formula_version TEXT NOT NULL,
    killmails_total BIGINT NOT NULL,
    killmails_valued BIGINT NOT NULL,
    fallback_price_pairs BIGINT NOT NULL,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE market.killmail_valuation_months IS
 'Small monthly checkpoints for the in-place rawkm.killmails estimated value calculation.';
