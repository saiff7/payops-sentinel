-- PayOps Sentinel schema v1. Money = integer cents. Rates = basis points. Dates = ISO-8601 text.
-- All demonstration data is SYNTHETIC_DEMO.

CREATE TABLE id_sequences (
    name        TEXT PRIMARY KEY,
    next_value  INTEGER NOT NULL
);

-- ---------------------------------------------------------------- reference data
CREATE TABLE employers (
    employer_id          TEXT PRIMARY KEY,
    employer_code        TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    data_classification  TEXT NOT NULL CHECK (data_classification = 'SYNTHETIC_DEMO')
);

CREATE TABLE participants (
    participant_id    TEXT PRIMARY KEY,
    employer_id       TEXT NOT NULL REFERENCES employers(employer_id),
    employee_ref      TEXT NOT NULL CHECK (employee_ref GLOB 'EMP-[0-9][0-9][0-9][0-9]'),
    termination_date  TEXT,
    UNIQUE (employer_id, employee_ref)
);

CREATE TABLE plan_enrollments (
    enrollment_id    TEXT PRIMARY KEY,
    participant_id   TEXT NOT NULL REFERENCES participants(participant_id),
    plan_id          TEXT NOT NULL,
    elected_rate_bp  INTEGER NOT NULL CHECK (elected_rate_bp BETWEEN 0 AND 10000),
    effective_from   TEXT NOT NULL,
    effective_to     TEXT,
    status           TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);
CREATE INDEX ix_enrollments_participant ON plan_enrollments(participant_id);

CREATE TABLE plan_rules (
    rule_id           TEXT PRIMARY KEY,
    ruleset_version   TEXT NOT NULL,
    plan_id           TEXT NOT NULL,
    rule_code         TEXT NOT NULL,
    version           TEXT NOT NULL,
    effective_from    TEXT NOT NULL,
    effective_to      TEXT,
    rule_json         TEXT NOT NULL,
    description       TEXT NOT NULL,
    source_reference  TEXT NOT NULL,
    UNIQUE (ruleset_version, plan_id, rule_code, version)
);

-- ---------------------------------------------------------------- ingestion
CREATE TABLE payroll_batches (
    batch_id             TEXT PRIMARY KEY,
    employer_id          TEXT NOT NULL REFERENCES employers(employer_id),
    file_sha256          TEXT NOT NULL CHECK (length(file_sha256) = 64),
    file_name            TEXT NOT NULL,
    ruleset_version      TEXT NOT NULL,
    pay_period_start     TEXT,
    pay_period_end       TEXT,
    status               TEXT NOT NULL CHECK (status IN ('processing', 'processed')),
    data_classification  TEXT NOT NULL CHECK (data_classification = 'SYNTHETIC_DEMO'),
    total_rows           INTEGER NOT NULL DEFAULT 0,
    valid_rows           INTEGER NOT NULL DEFAULT 0,
    invalid_rows         INTEGER NOT NULL DEFAULT 0,
    exception_count      INTEGER NOT NULL DEFAULT 0,
    duration_ms          INTEGER,
    created_at           TEXT NOT NULL,
    UNIQUE (employer_id, file_sha256, ruleset_version)
);

-- Only format-validated values are stored; raw untrusted text is never persisted.
CREATE TABLE payroll_rows (
    row_id               INTEGER PRIMARY KEY,
    batch_id             TEXT NOT NULL REFERENCES payroll_batches(batch_id),
    source_row_number    INTEGER NOT NULL,
    employee_ref         TEXT CHECK (employee_ref IS NULL OR employee_ref GLOB 'EMP-[0-9][0-9][0-9][0-9]'),
    pay_period_start     TEXT,
    pay_period_end       TEXT,
    eligible_comp_cents  INTEGER,
    contribution_cents   INTEGER,
    validation_status    TEXT NOT NULL CHECK (validation_status IN ('VALID', 'INVALID')),
    UNIQUE (batch_id, source_row_number)
);

CREATE TABLE validation_errors (
    error_id           INTEGER PRIMARY KEY,
    batch_id           TEXT NOT NULL REFERENCES payroll_batches(batch_id),
    row_id             INTEGER NOT NULL REFERENCES payroll_rows(row_id),
    source_row_number  INTEGER NOT NULL,
    field              TEXT NOT NULL,
    reason_code        TEXT NOT NULL,
    message            TEXT NOT NULL
);
CREATE INDEX ix_validation_errors_batch ON validation_errors(batch_id);

-- ---------------------------------------------------------------- reconciliation
CREATE TABLE exceptions (
    exception_id    TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES payroll_batches(batch_id),
    row_id          INTEGER NOT NULL REFERENCES payroll_rows(row_id),
    exception_type  TEXT NOT NULL CHECK (exception_type IN (
        'MISSING_CONTRIBUTION', 'INCORRECT_CONTRIBUTION_RATE',
        'CONTRIBUTION_AFTER_TERMINATION', 'DUPLICATE_OR_INVALID_RECORD')),
    severity        TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    reason_code     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    employee_ref    TEXT,
    pay_period_end  TEXT,
    expected_cents  INTEGER,
    actual_cents    INTEGER,
    rule_id         TEXT REFERENCES plan_rules(rule_id),
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'under_investigation', 'manual_review', 'closed')),
    created_at      TEXT NOT NULL,
    UNIQUE (row_id, exception_type)
);
CREATE INDEX ix_exceptions_batch ON exceptions(batch_id);

CREATE TABLE evidence (
    evidence_id       TEXT PRIMARY KEY,
    exception_id      TEXT NOT NULL REFERENCES exceptions(exception_id),
    evidence_type     TEXT NOT NULL,
    source_reference  TEXT NOT NULL,
    snapshot_json     TEXT NOT NULL,
    content_hash      TEXT NOT NULL CHECK (length(content_hash) = 64),
    created_at        TEXT NOT NULL
);
CREATE INDEX ix_evidence_exception ON evidence(exception_id);

-- ---------------------------------------------------------------- agent (Phase 2)
CREATE TABLE agent_runs (
    run_id          TEXT PRIMARY KEY,
    batch_id        TEXT REFERENCES payroll_batches(batch_id),
    exception_id    TEXT REFERENCES exceptions(exception_id),
    agent_version   TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    status          TEXT NOT NULL,
    error_category  TEXT,
    started_at      TEXT NOT NULL,
    duration_ms     INTEGER
);

CREATE TABLE tool_events (
    event_id        INTEGER PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES agent_runs(run_id),
    sequence        INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    arguments_json  TEXT NOT NULL,
    result_hash     TEXT,
    duration_ms     INTEGER,
    success         INTEGER NOT NULL CHECK (success IN (0, 1)),
    error_category  TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (run_id, sequence)
);

-- ---------------------------------------------------------------- review (Phase 3)
CREATE TABLE drafts (
    draft_id       TEXT NOT NULL,
    draft_version  INTEGER NOT NULL CHECK (draft_version >= 1),
    exception_id   TEXT NOT NULL REFERENCES exceptions(exception_id),
    run_id         TEXT REFERENCES agent_runs(run_id),
    author_id      TEXT NOT NULL,
    draft_json     TEXT NOT NULL,
    content_hash   TEXT NOT NULL CHECK (length(content_hash) = 64),
    status         TEXT NOT NULL
                   CHECK (status IN ('pending_review', 'superseded', 'approved', 'rejected')),
    created_at     TEXT NOT NULL,
    PRIMARY KEY (draft_id, draft_version)
);

CREATE TRIGGER drafts_content_immutable BEFORE UPDATE OF draft_json, content_hash, exception_id ON drafts
BEGIN
    SELECT RAISE(ABORT, 'draft content is immutable; create a new version');
END;

CREATE TABLE approvals (
    approval_id    TEXT PRIMARY KEY,
    draft_id       TEXT NOT NULL,
    draft_version  INTEGER NOT NULL,
    reviewer_id    TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('approve', 'reject')),
    comment        TEXT,
    content_hash   TEXT NOT NULL CHECK (length(content_hash) = 64),
    created_at     TEXT NOT NULL,
    FOREIGN KEY (draft_id, draft_version) REFERENCES drafts(draft_id, draft_version)
);

CREATE TRIGGER approvals_no_update BEFORE UPDATE ON approvals
BEGIN
    SELECT RAISE(ABORT, 'approvals are append-only');
END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
BEGIN
    SELECT RAISE(ABORT, 'approvals are append-only');
END;

-- Tamper-evident hash chain: event_hash = SHA256(previous_hash || canonical_event).
CREATE TABLE audit_events (
    sequence       INTEGER PRIMARY KEY,
    event_id       TEXT NOT NULL UNIQUE,
    actor_id       TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    previous_hash  TEXT NOT NULL,
    event_hash     TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL
);

CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events are append-only');
END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events are append-only');
END;

-- ---------------------------------------------------------------- operations (Phases 3-4)
CREATE TABLE agent_assets (
    asset_id           TEXT PRIMARY KEY,
    asset_name         TEXT NOT NULL,
    agent_version      TEXT NOT NULL,
    model              TEXT NOT NULL,
    model_digest       TEXT,
    prompt_version     TEXT NOT NULL,
    technical_owner    TEXT NOT NULL,
    business_owner     TEXT NOT NULL,
    backup_owner       TEXT NOT NULL,
    environment        TEXT NOT NULL,
    status             TEXT NOT NULL,
    evaluation_status  TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE evaluation_runs (
    evaluation_id   TEXT PRIMARY KEY,
    model_version   TEXT NOT NULL,
    model_digest    TEXT,
    agent_version   TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    tools_version   TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    metrics_json    TEXT NOT NULL,
    passed          INTEGER NOT NULL CHECK (passed IN (0, 1))
);

CREATE TABLE evaluation_cases (
    evaluation_id   TEXT NOT NULL REFERENCES evaluation_runs(evaluation_id),
    case_id         TEXT NOT NULL,
    expected_tools  TEXT NOT NULL,
    actual_tools    TEXT NOT NULL,
    score           REAL NOT NULL,
    passed          INTEGER NOT NULL CHECK (passed IN (0, 1)),
    details_json    TEXT NOT NULL,
    PRIMARY KEY (evaluation_id, case_id)
);

PRAGMA user_version = 1;
