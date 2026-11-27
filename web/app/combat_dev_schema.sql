CREATE SCHEMA IF NOT EXISTS devencour;
CREATE TABLE IF NOT EXISTS devencour.collector_tokens (
 owner bigint PRIMARY KEY, digest text UNIQUE NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS devencour.death_summaries (
 owner bigint NOT NULL, summary_id text NOT NULL, character_id bigint NOT NULL,
 observer text NOT NULL, occurred_at timestamptz NOT NULL, payload jsonb NOT NULL,
 received_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(owner,summary_id)
);
CREATE INDEX IF NOT EXISTS death_summaries_character_time ON devencour.death_summaries(character_id,occurred_at);
CREATE TABLE IF NOT EXISTS devencour.received_healing (
 owner bigint NOT NULL, summary_id text NOT NULL, healer_index integer NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(owner,summary_id,healer_index),
 FOREIGN KEY(owner,summary_id) REFERENCES devencour.death_summaries(owner,summary_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS devencour.summary_killmail_links (
 owner bigint NOT NULL, summary_id text NOT NULL, killmail_id bigint NOT NULL,
 linked_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(owner,summary_id),
 FOREIGN KEY(owner,summary_id) REFERENCES devencour.death_summaries(owner,summary_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS summary_killmail_links_kill ON devencour.summary_killmail_links(killmail_id);
CREATE TABLE IF NOT EXISTS devencour.raw_events (
 owner bigint NOT NULL, event_id text NOT NULL, observer text NOT NULL, character_id bigint NOT NULL,
 occurred_at timestamptz NOT NULL, kind text NOT NULL, payload jsonb NOT NULL,
 received_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(owner,event_id)
);
ALTER TABLE devencour.raw_events ADD COLUMN IF NOT EXISTS processed_at timestamptz;
CREATE INDEX IF NOT EXISTS raw_events_time ON devencour.raw_events(occurred_at DESC,event_id DESC,owner DESC);
CREATE INDEX IF NOT EXISTS raw_events_observer_time ON devencour.raw_events(observer,occurred_at DESC,event_id DESC,owner DESC);
CREATE INDEX IF NOT EXISTS raw_events_kind_time ON devencour.raw_events(kind,occurred_at DESC);
CREATE INDEX IF NOT EXISTS raw_events_character_time ON devencour.raw_events(character_id,occurred_at);
CREATE INDEX IF NOT EXISTS raw_events_target_time ON devencour.raw_events(lower(payload->'target'->>'name'),occurred_at)
 WHERE kind IN ('shield_out','armor_out','hull_out','capacitor_out');
-- Compact scores only: no permanent list of assisted recipients or transfers.
CREATE TABLE IF NOT EXISTS devencour.killmail_support (
 killmail_id bigint NOT NULL, side text NOT NULL CHECK(side IN ('defense','attack')),
 resource text NOT NULL CHECK(resource IN ('healing','capacitor')), supporter_key text NOT NULL,
 amount bigint NOT NULL CHECK(amount>0), source jsonb NOT NULL,
 calculated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(killmail_id,side,resource,supporter_key)
);
CREATE TABLE IF NOT EXISTS devencour.replay_requests (
 id bigserial PRIMARY KEY, owner bigint NOT NULL, character_id bigint,
 date_from timestamptz NOT NULL, date_to timestamptz NOT NULL CHECK(date_to>=date_from),
 active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- Debug killboard snapshots, populated only by the explicit recalculation action.
CREATE TABLE IF NOT EXISTS devencour.killboard_entries (
 owner bigint NOT NULL, observer text NOT NULL, character_id bigint NOT NULL, killmail_id bigint NOT NULL,
 occurred_at timestamptz NOT NULL, side text NOT NULL, ship_type_id bigint, ship_name text, is_pod boolean,
 PRIMARY KEY(owner,observer,killmail_id)
);
CREATE TABLE IF NOT EXISTS devencour.killboard_scans (
 owner bigint NOT NULL, observer text NOT NULL, events bigint NOT NULL, checked_at timestamptz NOT NULL,
 status text NOT NULL, PRIMARY KEY(owner,observer)
);
CREATE TABLE IF NOT EXISTS devencour.collector_streams (
 owner bigint NOT NULL, observer text NOT NULL, character_id bigint NOT NULL,
 events bigint NOT NULL DEFAULT 0, first_at timestamptz NOT NULL, last_at timestamptz NOT NULL,
 received_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(owner,observer)
);
