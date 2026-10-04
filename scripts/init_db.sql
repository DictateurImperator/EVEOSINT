-- =========================================================
-- EVEOSINT - INITIAL DATABASE SCHEMA (POSTGRESQL)
-- =========================================================

---

-- META / VERSIONING

---

CREATE TABLE IF NOT EXISTS app_meta (
key TEXT PRIMARY KEY,
value TEXT
);

INSERT INTO app_meta (key, value)
VALUES ('db_version', '1')
ON CONFLICT (key) DO NOTHING;

---

-- SDE TRACKING

---

CREATE TABLE IF NOT EXISTS sde_import_runs (
id SERIAL PRIMARY KEY,
build_number INTEGER,
variant TEXT,
status TEXT,
started_at DOUBLE PRECISION,
finished_at DOUBLE PRECISION,
error TEXT
);

CREATE TABLE IF NOT EXISTS sde_remote_resources (
url TEXT PRIMARY KEY,
etag TEXT,
last_modified TEXT,
last_checked_at DOUBLE PRECISION,
last_status INTEGER
);

---

-- INDEXES

---

CREATE INDEX IF NOT EXISTS idx_sde_runs_build
ON sde_import_runs(build_number);

CREATE INDEX IF NOT EXISTS idx_sde_resources_checked
ON sde_remote_resources(last_checked_at);

---

-- FUTURE SDE TABLES

---

-- Elles seront créées dynamiquement à l'import
-- ex:
-- sde_types
-- sde_groups
-- sde_categories
-- sde_mapSolarSystems





CREATE SCHEMA IF NOT EXISTS rawkm;

SET search_path TO rawkm, public;







CREATE TABLE IF NOT EXISTS rawkm.killmails (
    killmail_id BIGINT PRIMARY KEY,
    killmail_hash TEXT,
    killmail_time TIMESTAMPTZ NOT NULL,
    solar_system_id BIGINT NOT NULL,
    victim_character_id BIGINT,
    victim_corporation_id BIGINT,
    victim_alliance_id BIGINT,
    victim_ship_type_id BIGINT,
    victim_damage_taken INTEGER,
    raw_json JSONB NOT NULL,
    source_url TEXT,
    archive_path TEXT,
    http_last_modified TIMESTAMPTZ,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rawkm.killmail_attackers (
    killmail_id BIGINT NOT NULL,
    attacker_index INTEGER NOT NULL,
    character_id BIGINT,
    corporation_id BIGINT,
    alliance_id BIGINT,
    ship_type_id BIGINT,
    weapon_type_id BIGINT,
    damage_done INTEGER,
    final_blow BOOLEAN,
    security_status DOUBLE PRECISION,
    PRIMARY KEY (killmail_id, attacker_index)
);

CREATE TABLE IF NOT EXISTS rawkm.killmail_items (
    killmail_id BIGINT NOT NULL,
    item_index INTEGER NOT NULL,
    item_type_id BIGINT,
    flag INTEGER,
    quantity_destroyed BIGINT,
    quantity_dropped BIGINT,
    singleton INTEGER,
    raw_json JSONB NOT NULL,
    PRIMARY KEY (killmail_id, item_index)
);

CREATE TABLE IF NOT EXISTS rawkm.killmail_import_days (
    day DATE PRIMARY KEY,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL,
    files_count INTEGER,
    archive_count INTEGER,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    error TEXT
);

---

-- SOVEREIGNTY INFLUENCE COLORS

---

CREATE SCHEMA IF NOT EXISTS sovereignty;

CREATE TABLE IF NOT EXISTS sovereignty.reconciled_map (
    event_id BIGSERIAL PRIMARY KEY,
    system_id BIGINT NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('GAIN', 'LOST')),
    alliance_id BIGINT,
    corporation_id BIGINT,
    faction_id BIGINT,
    source TEXT NOT NULL CHECK (source IN ('dotlan', 'esi')),
    ownership_model TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (system_id, event_at, action, source)
);

CREATE INDEX IF NOT EXISTS sov_reconciled_map_system_event_idx
ON sovereignty.reconciled_map (system_id, event_at DESC, event_id DESC);

CREATE INDEX IF NOT EXISTS sov_reconciled_map_event_idx
ON sovereignty.reconciled_map (event_at DESC);

CREATE TABLE IF NOT EXISTS sovereignty.influence_color_assignments (
    assignment_id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('alliance', 'coalition')),
    entity_id BIGINT NOT NULL,
    color TEXT NOT NULL,
    color_hue DOUBLE PRECISION NOT NULL,
    color_saturation DOUBLE PRECISION NOT NULL,
    color_lightness DOUBLE PRECISION NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS sov_influence_color_active_entity_idx
ON sovereignty.influence_color_assignments (entity_type, entity_id)
WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS sov_influence_color_history_idx
ON sovereignty.influence_color_assignments (entity_type, entity_id, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS sovereignty.influence_color_state (
    entity_type TEXT PRIMARY KEY CHECK (entity_type IN ('alliance', 'coalition')),
    initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sovereignty.influence_color_overrides (
    entity_type TEXT NOT NULL CHECK (entity_type IN ('alliance', 'coalition')),
    entity_id BIGINT NOT NULL,
    color TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by BIGINT,
    PRIMARY KEY (entity_type, entity_id)
);

