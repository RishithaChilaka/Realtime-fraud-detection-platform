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
