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
CREATE INDEX IF NOT EXISTS raw_events_time ON devencour.raw_events(occurred_at DESC,event_id DESC,owner DESC);
CREATE INDEX IF NOT EXISTS raw_events_observer_time ON devencour.raw_events(observer,occurred_at DESC,event_id DESC,owner DESC);
CREATE INDEX IF NOT EXISTS raw_events_kind_time ON devencour.raw_events(kind,occurred_at DESC);
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
