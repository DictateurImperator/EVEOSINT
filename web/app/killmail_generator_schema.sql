CREATE SCHEMA IF NOT EXISTS "KillmailGenerator";
CREATE TABLE IF NOT EXISTS "KillmailGenerator".collector_tokens (
    user_id bigint PRIMARY KEY REFERENCES web.users(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS "KillmailGenerator".repair_reports (
    owner_user_id bigint NOT NULL REFERENCES web.users(id) ON DELETE CASCADE,
    report_id text NOT NULL CHECK (report_id ~ '^[0-9a-f]{64}$'),
    victim_pilot text NOT NULL,
    victim_ship text,
    loss_time timestamptz NOT NULL,
    window_seconds integer NOT NULL CHECK (window_seconds = 1800),
    partial boolean NOT NULL,
    logi jsonb NOT NULL CHECK (jsonb_typeof(logi) = 'array'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    validation_status text NOT NULL DEFAULT 'pending_validation'
        CHECK (validation_status IN ('pending_validation', 'confirmed', 'rejected')),
    killmail_id bigint,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_user_id, report_id)
);
CREATE INDEX IF NOT EXISTS repair_reports_victim_time_idx
    ON "KillmailGenerator".repair_reports (victim_pilot, loss_time);
