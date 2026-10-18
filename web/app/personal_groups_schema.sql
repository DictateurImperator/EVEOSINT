-- Definitions and access rights only. Group results are computed on request.
CREATE TABLE IF NOT EXISTS web.personal_groups (
 id bigserial PRIMARY KEY,
 owner_id bigint NOT NULL REFERENCES web.users(id) ON DELETE CASCADE,
 slot smallint NOT NULL CHECK(slot BETWEEN 1 AND 3),
 name text NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
 description text NOT NULL DEFAULT '',
 definition jsonb NOT NULL,
 revision bigint NOT NULL DEFAULT 1,
 public_token text UNIQUE,
 public_tabs jsonb NOT NULL DEFAULT '["killboard","stats","ships","ranking"]',
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(owner_id,slot)
);
-- Retire only the old derived group summaries; preserve definitions, links and delegates.
DROP TABLE IF EXISTS web.personal_group_periods;
ALTER TABLE web.personal_groups
 DROP COLUMN IF EXISTS prepared_revision,
 DROP COLUMN IF EXISTS prepared_rules,
 DROP COLUMN IF EXISTS prepared_at,
 DROP COLUMN IF EXISTS progress;

CREATE TABLE IF NOT EXISTS web.personal_group_managers (
 group_id bigint NOT NULL REFERENCES web.personal_groups(id) ON DELETE CASCADE,
 user_id bigint NOT NULL REFERENCES web.users(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(group_id,user_id)
);
CREATE INDEX IF NOT EXISTS personal_group_managers_user_idx ON web.personal_group_managers(user_id,group_id);
