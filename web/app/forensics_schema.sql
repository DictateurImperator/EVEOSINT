-- Run explicitly through Admin Jobs; never on web startup.
CREATE TABLE IF NOT EXISTS web.forensics_cases (
    id BIGSERIAL PRIMARY KEY,
    kill_datetime TIMESTAMPTZ NOT NULL,
    source_month DATE NOT NULL,
    source_row BIGINT NOT NULL,
    snapshot JSONB NOT NULL,
    hypotheses JSONB NOT NULL,
    choices JSONB NOT NULL,
    plan_cursor INTEGER NOT NULL DEFAULT 0,
    estimated_attempts BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready',
    recovered_killmail_id BIGINT,
    created_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(kill_datetime, source_month, source_row),
    CHECK(status IN ('ready','needs_input','exhausted','recovered')),
    CHECK((status='recovered')=(recovered_killmail_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS web.forensics_attempts (
    case_id BIGINT NOT NULL REFERENCES web.forensics_cases(id),
    killmail_id BIGINT NOT NULL,
    hash TEXT NOT NULL CHECK(hash ~ '^[0-9a-f]{40}$'),
    victim_id BIGINT,
    attacker_id BIGINT,
    score DOUBLE PRECISION NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('invalid','mismatch','recovered')),
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(case_id,killmail_id,hash)
);
CREATE TABLE IF NOT EXISTS web.forensics_recovered (
    killmail_id BIGINT PRIMARY KEY,
    hash TEXT NOT NULL CHECK(hash ~ '^[0-9a-f]{40}$'),
    case_id BIGINT NOT NULL UNIQUE REFERENCES web.forensics_cases(id),
    payload JSONB NOT NULL,
    confirmed_by BIGINT,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS web.forensics_esi_gate (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    blocked_until TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO web.forensics_esi_gate(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS web.forensics_esi_requests (
    id BIGSERIAL PRIMARY KEY,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cost INTEGER NOT NULL DEFAULT 5
);
CREATE INDEX IF NOT EXISTS forensics_cases_attempts_idx ON web.forensics_cases(estimated_attempts,id);
CREATE INDEX IF NOT EXISTS forensics_attempts_hash_idx ON web.forensics_attempts(killmail_id,hash);
CREATE INDEX IF NOT EXISTS forensics_esi_requests_at_idx ON web.forensics_esi_requests(reserved_at);

-- Upgrade existing installations through the same explicit Admin Jobs migration.
ALTER TABLE web.forensics_cases ADD COLUMN IF NOT EXISTS forecast JSONB;
ALTER TABLE web.forensics_cases ADD COLUMN IF NOT EXISTS expected_trials DOUBLE PRECISION;
ALTER TABLE web.forensics_cases ADD COLUMN IF NOT EXISTS recovery_priority INTEGER;
ALTER TABLE web.forensics_cases ADD COLUMN IF NOT EXISTS analysis_error TEXT;
CREATE TABLE IF NOT EXISTS web.forensics_analysis_runs (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('running','complete','stopped','failed')),
    date_from DATE,
    date_to DATE,
    created_by BIGINT,
    total BIGINT,
    analyzed BIGINT NOT NULL DEFAULT 0,
    failed BIGINT NOT NULL DEFAULT 0,
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
    message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
ALTER TABLE web.forensics_cases ADD COLUMN IF NOT EXISTS choices_customized BOOLEAN NOT NULL DEFAULT TRUE;
CREATE INDEX IF NOT EXISTS forensics_cases_time_idx ON web.forensics_cases(kill_datetime,id);
CREATE TABLE IF NOT EXISTS web.forensics_refresh_queue (
    case_id BIGINT PRIMARY KEY REFERENCES web.forensics_cases(id),
    trigger_killmail_id BIGINT NOT NULL,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    failures INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE web.forensics_analysis_runs ADD COLUMN IF NOT EXISTS refreshed BIGINT NOT NULL DEFAULT 0;
ALTER TABLE web.forensics_analysis_runs ADD COLUMN IF NOT EXISTS refresh_only BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS forensics_cases_cost_order_idx ON web.forensics_cases
    ((CASE WHEN status='ready' AND estimated_attempts>0 THEN 0 ELSE 1 END),expected_trials,id)
    WHERE status<>'recovered';
CREATE INDEX IF NOT EXISTS forensics_cases_priority_order_idx ON web.forensics_cases
    ((CASE WHEN status='ready' AND estimated_attempts>0 THEN 0 ELSE 1 END),recovery_priority DESC NULLS LAST,expected_trials,id)
    WHERE status<>'recovered';
CREATE INDEX IF NOT EXISTS forensics_cases_remaining_order_idx ON web.forensics_cases
    ((CASE WHEN status='ready' AND estimated_attempts>0 THEN 0 ELSE 1 END),estimated_attempts,id)
    WHERE status<>'recovered';
CREATE INDEX IF NOT EXISTS forensics_cases_pilot_evidence_idx ON web.forensics_cases
    USING gin((forecast->'candidate_evidence') jsonb_path_ops);
CREATE INDEX IF NOT EXISTS forensics_cases_context_evidence_idx ON web.forensics_cases
    USING gin((forecast->'context_flags') jsonb_path_ops);
ALTER TABLE web.forensics_cases ADD COLUMN IF NOT EXISTS analysis_version INTEGER;

CREATE TABLE IF NOT EXISTS web.forensics_zkill_submissions (
    killmail_id BIGINT PRIMARY KEY REFERENCES web.forensics_recovered(killmail_id),
    status TEXT NOT NULL CHECK(status IN ('accepted','failed','uncertain')),
    submitted_by BIGINT,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ NOT NULL,
    http_status INTEGER
);
ALTER TABLE web.forensics_esi_gate ADD COLUMN IF NOT EXISTS rate_limit JSONB;
