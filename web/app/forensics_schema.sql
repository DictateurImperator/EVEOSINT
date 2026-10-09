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
