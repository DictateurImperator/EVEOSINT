-- Small group definitions and prepared summaries only; never copies source killmails.
CREATE TABLE IF NOT EXISTS web.personal_groups (
 id bigserial PRIMARY KEY,
 owner_id bigint NOT NULL REFERENCES web.users(id) ON DELETE CASCADE,
 slot smallint NOT NULL CHECK(slot BETWEEN 1 AND 3),
 name text NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
 description text NOT NULL DEFAULT '',
 definition jsonb NOT NULL,
 revision bigint NOT NULL DEFAULT 1,
 prepared_revision bigint,
 prepared_rules jsonb NOT NULL DEFAULT '{}',
 prepared_at timestamptz,
 public_token text UNIQUE,
 public_tabs jsonb NOT NULL DEFAULT '["killboard","stats","ships"]',
 progress jsonb NOT NULL DEFAULT '{"state":"idle"}',
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(owner_id,slot)
);
CREATE TABLE IF NOT EXISTS web.personal_group_periods (
 group_id bigint NOT NULL REFERENCES web.personal_groups(id) ON DELETE CASCADE,
 revision bigint NOT NULL,
 period text NOT NULL CHECK(period='all' OR period='custom' OR period ~ '^\d{4}(-\d{2})?$'),
 from_date date NOT NULL,
 through_date date NOT NULL,
 totals jsonb NOT NULL,
 ship_counts jsonb NOT NULL DEFAULT '{}',
 roster_count bigint NOT NULL DEFAULT 0,
 skills_count bigint NOT NULL DEFAULT 0,
 built_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(group_id,period)
);
