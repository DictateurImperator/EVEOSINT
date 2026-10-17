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
