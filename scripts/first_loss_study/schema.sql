CREATE SCHEMA IF NOT EXISTS research;
CREATE TABLE IF NOT EXISTS research.first_loss_runs (
 id BIGSERIAL PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
 cutoff TIMESTAMPTZ NOT NULL, history_start DATE NOT NULL, config JSONB NOT NULL,
 phase TEXT NOT NULL DEFAULT 'losses', created_at TIMESTAMPTZ DEFAULT now(),
 updated_at TIMESTAMPTZ DEFAULT now(), model JSONB, summary JSONB, error TEXT
);
CREATE TABLE IF NOT EXISTS research.first_loss_months (
 run_id BIGINT REFERENCES research.first_loss_runs ON DELETE CASCADE,
 phase TEXT NOT NULL, month DATE NOT NULL, PRIMARY KEY(run_id,phase,month)
);
CREATE TABLE IF NOT EXISTS research.first_loss_players (
 run_id BIGINT REFERENCES research.first_loss_runs ON DELETE CASCADE,
 character_id BIGINT NOT NULL, loss_time TIMESTAMPTZ NOT NULL, loss_id BIGINT NOT NULL,
 loss_month DATE NOT NULL,
 corporation_id BIGINT, alliance_id BIGINT, ship_type_id BIGINT, system_id BIGINT,
 prior_attacks BIGINT NOT NULL DEFAULT 0, prior_attack_days INTEGER NOT NULL DEFAULT 0,
 first_return TIMESTAMPTZ, return_ship BIGINT, return_system BIGINT, return_role TEXT,
 first_pvp TIMESTAMPTZ, first_attack TIMESTAMPTZ, next_loss TIMESTAMPTZ,
 losses_30 INTEGER NOT NULL DEFAULT 0, attacks_30 INTEGER NOT NULL DEFAULT 0,
 losses_90 INTEGER NOT NULL DEFAULT 0, attacks_90 INTEGER NOT NULL DEFAULT 0,
 features JSONB, outcomes JSONB, cohort SMALLINT, cluster SMALLINT, projection JSONB,
 PRIMARY KEY(run_id,character_id)
);
CREATE INDEX IF NOT EXISTS first_loss_players_date_idx ON research.first_loss_players(run_id,loss_time);
CREATE INDEX IF NOT EXISTS first_loss_players_feature_batch_idx ON research.first_loss_players(run_id,loss_month,character_id);
CREATE INDEX IF NOT EXISTS first_loss_players_cluster_idx ON research.first_loss_players(run_id,cluster,cohort);
CREATE TABLE IF NOT EXISTS research.first_loss_group_days (
 run_id BIGINT REFERENCES research.first_loss_runs ON DELETE CASCADE,
 day DATE NOT NULL, kind TEXT NOT NULL, entity_id BIGINT NOT NULL,
 attack_events BIGINT NOT NULL DEFAULT 0, losses BIGINT NOT NULL DEFAULT 0,
 industry_losses BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(run_id,kind,entity_id,day)
);
