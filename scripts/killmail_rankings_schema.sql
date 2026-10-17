-- Sparse monthly facts only: no cumulative totals, no per-kill ranking copies.
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE TABLE IF NOT EXISTS analytics.killmail_ranking_versions (
    month date PRIMARY KEY CHECK (extract(day FROM month)=1),
    revision bigint NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS analytics.killmail_ranking_months (
    month date PRIMARY KEY, built_revision bigint NOT NULL,
    built_at timestamptz NOT NULL DEFAULT now(),
    killmails bigint NOT NULL, valued bigint NOT NULL,
    zero_damage bigint NOT NULL, entity_rows bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics.killmail_rankings_monthly (
    month date NOT NULL, entity_kind smallint NOT NULL CHECK(entity_kind BETWEEN 1 AND 3),
    entity_id bigint NOT NULL CHECK(entity_id>0),
    destroyed_cents numeric(30,0) NOT NULL DEFAULT 0,
    weighted_destroyed_cents numeric(38,8) NOT NULL DEFAULT 0,
    lost_cents numeric(30,0) NOT NULL DEFAULT 0,
    kills bigint NOT NULL DEFAULT 0, losses bigint NOT NULL DEFAULT 0,
    unvalued_kills bigint NOT NULL DEFAULT 0, unvalued_losses bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (month,entity_kind,entity_id),
    CHECK(kills>0 OR losses>0)
);

-- One current total per active entity. Historical changes are applied as monthly deltas.
CREATE TABLE IF NOT EXISTS analytics.killmail_rankings_current (
    entity_kind smallint NOT NULL, entity_id bigint NOT NULL,
    destroyed_cents numeric(30,0) NOT NULL DEFAULT 0,
    weighted_destroyed_cents numeric(38,8) NOT NULL DEFAULT 0,
    lost_cents numeric(30,0) NOT NULL DEFAULT 0,
    kills bigint NOT NULL DEFAULT 0, losses bigint NOT NULL DEFAULT 0,
    unvalued_kills bigint NOT NULL DEFAULT 0, unvalued_losses bigint NOT NULL DEFAULT 0,
    PRIMARY KEY(entity_kind,entity_id), CHECK(kills>=0 AND losses>=0)
);
CREATE TABLE IF NOT EXISTS analytics.killmail_ranking_current_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    ready boolean NOT NULL DEFAULT false
);
INSERT INTO analytics.killmail_ranking_current_state VALUES(true,false) ON CONFLICT DO NOTHING;
CREATE INDEX IF NOT EXISTS ranking_current_destroyed ON analytics.killmail_rankings_current
    (entity_kind,destroyed_cents DESC NULLS LAST,entity_id);
CREATE INDEX IF NOT EXISTS ranking_current_weighted ON analytics.killmail_rankings_current
    (entity_kind,weighted_destroyed_cents DESC NULLS LAST,entity_id);
CREATE INDEX IF NOT EXISTS ranking_current_lost ON analytics.killmail_rankings_current
    (entity_kind,lost_cents DESC NULLS LAST,entity_id);

-- Cumulative snapshots exist only on the already sparse activity rows.
ALTER TABLE analytics.killmail_rankings_monthly ADD COLUMN IF NOT EXISTS cumulative_destroyed_cents numeric(30,0);
ALTER TABLE analytics.killmail_rankings_monthly ADD COLUMN IF NOT EXISTS cumulative_weighted_destroyed_cents numeric(38,8);
ALTER TABLE analytics.killmail_rankings_monthly ADD COLUMN IF NOT EXISTS cumulative_lost_cents numeric(30,0);
ALTER TABLE analytics.killmail_rankings_monthly ADD COLUMN IF NOT EXISTS cumulative_kills bigint;
ALTER TABLE analytics.killmail_rankings_monthly ADD COLUMN IF NOT EXISTS cumulative_losses bigint;
ALTER TABLE analytics.killmail_rankings_monthly ADD COLUMN IF NOT EXISTS cumulative_unvalued_kills bigint;
ALTER TABLE analytics.killmail_rankings_monthly ADD COLUMN IF NOT EXISTS cumulative_unvalued_losses bigint;
CREATE INDEX IF NOT EXISTS ranking_monthly_latest ON analytics.killmail_rankings_monthly
    (entity_kind,entity_id,month DESC);
ALTER TABLE analytics.killmail_rankings_current ADD COLUMN IF NOT EXISTS last_month date;
