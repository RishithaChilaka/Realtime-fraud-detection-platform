-- Executed automatically by the postgres container on first boot
-- (mounted into /docker-entrypoint-initdb.d). Mirrors src/storage/postgres_models.py;
-- kept as a plain SQL file too so the schema is visible without running Python.

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id      VARCHAR(64) PRIMARY KEY,
    card_id             VARCHAR(64) NOT NULL,
    user_id             VARCHAR(64) NOT NULL,
    amount              DOUBLE PRECISION NOT NULL,
    currency            VARCHAR(3) NOT NULL,
    merchant_id         VARCHAR(64) NOT NULL,
    merchant_category   VARCHAR(32) NOT NULL,
    transaction_type    VARCHAR(16) NOT NULL,
    channel             VARCHAR(16) NOT NULL,
    latitude            DOUBLE PRECISION NOT NULL,
    longitude           DOUBLE PRECISION NOT NULL,
    country             VARCHAR(2) NOT NULL,
    device_id           VARCHAR(64),
    ip_address          VARCHAR(45),
    event_time          TIMESTAMPTZ NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_simulated_fraud  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_transactions_card_id ON transactions (card_id);
CREATE INDEX IF NOT EXISTS ix_transactions_user_id ON transactions (user_id);
CREATE INDEX IF NOT EXISTS ix_transactions_card_event_time ON transactions (card_id, event_time);

CREATE TABLE IF NOT EXISTS audit_logs (
    id              UUID PRIMARY KEY,
    event_type      VARCHAR(64) NOT NULL,
    transaction_id  VARCHAR(64),
    severity        VARCHAR(16) NOT NULL DEFAULT 'info',
    message         VARCHAR(1024) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_logs_event_type ON audit_logs (event_type);
CREATE INDEX IF NOT EXISTS ix_audit_logs_transaction_id ON audit_logs (transaction_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);

-- ---------------------------------------------------------------------------
-- Phase 2: ML inference audit trail, human-in-the-loop review, and model
-- governance. Mirrors src/storage/postgres_models.py.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS predictions (
    id                UUID PRIMARY KEY,
    transaction_id    VARCHAR(64) NOT NULL,
    card_id           VARCHAR(64) NOT NULL,
    model_name        VARCHAR(64) NOT NULL,
    model_version     VARCHAR(32) NOT NULL,
    model_source      VARCHAR(16) NOT NULL DEFAULT 'ml',
    input_features    JSONB NOT NULL,
    fraud_score       DOUBLE PRECISION NOT NULL,
    risk_level        VARCHAR(16) NOT NULL,
    decision          VARCHAR(16) NOT NULL,
    routed_to_review  BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms        DOUBLE PRECISION NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_predictions_transaction_id ON predictions (transaction_id);
CREATE INDEX IF NOT EXISTS ix_predictions_card_id ON predictions (card_id);
CREATE INDEX IF NOT EXISTS ix_predictions_created_at ON predictions (created_at);

CREATE TABLE IF NOT EXISTS review_cases (
    id                 UUID PRIMARY KEY,
    prediction_id      UUID NOT NULL,
    transaction_id     VARCHAR(64) NOT NULL,
    fraud_score        DOUBLE PRECISION NOT NULL,
    risk_level         VARCHAR(16) NOT NULL,
    reason             VARCHAR(256) NOT NULL,
    status             VARCHAR(16) NOT NULL DEFAULT 'pending',
    assigned_analyst   VARCHAR(64),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_review_cases_prediction_id ON review_cases (prediction_id);
CREATE INDEX IF NOT EXISTS ix_review_cases_transaction_id ON review_cases (transaction_id);
CREATE INDEX IF NOT EXISTS ix_review_cases_status ON review_cases (status);
CREATE INDEX IF NOT EXISTS ix_review_cases_created_at ON review_cases (created_at);

CREATE TABLE IF NOT EXISTS analyst_feedback (
    id               UUID PRIMARY KEY,
    case_id          UUID NOT NULL,
    transaction_id   VARCHAR(64) NOT NULL,
    analyst_id       VARCHAR(64) NOT NULL,
    label            VARCHAR(24) NOT NULL,
    notes            VARCHAR(1024),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_analyst_feedback_case_id ON analyst_feedback (case_id);
CREATE INDEX IF NOT EXISTS ix_analyst_feedback_transaction_id ON analyst_feedback (transaction_id);
CREATE INDEX IF NOT EXISTS ix_analyst_feedback_created_at ON analyst_feedback (created_at);

CREATE TABLE IF NOT EXISTS model_approvals (
    id                  UUID PRIMARY KEY,
    model_name          VARCHAR(64) NOT NULL,
    model_version       VARCHAR(32) NOT NULL,
    from_stage          VARCHAR(16) NOT NULL,
    to_stage            VARCHAR(16) NOT NULL,
    approved_by         VARCHAR(64) NOT NULL,
    notes               VARCHAR(1024),
    metrics_snapshot    JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_model_approvals_name_version ON model_approvals (model_name, model_version);

-- ---------------------------------------------------------------------------
-- Phase 3: drift detection audit trail. Mirrors DriftReportRecord.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS drift_reports (
    id                   UUID PRIMARY KEY,
    model_name           VARCHAR(64) NOT NULL,
    reference_window     VARCHAR(64) NOT NULL,
    current_window       VARCHAR(64) NOT NULL,
    any_drift_detected   BOOLEAN NOT NULL,
    drifted_features     JSONB NOT NULL,
    max_psi              DOUBLE PRECISION NOT NULL,
    score_ks_p_value     DOUBLE PRECISION,
    score_psi            DOUBLE PRECISION,
    score_drifted        BOOLEAN NOT NULL DEFAULT FALSE,
    full_report          JSONB NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_drift_reports_model_name ON drift_reports (model_name);
CREATE INDEX IF NOT EXISTS ix_drift_reports_created_at ON drift_reports (created_at);
