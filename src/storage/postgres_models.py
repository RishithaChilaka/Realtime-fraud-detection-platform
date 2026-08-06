"""SQLAlchemy ORM models: transaction persistence + audit trail.

Kept separate from `postgres_client.py` (connection/session management) so
the schema can be imported by Alembic migrations, the Spark JDBC sink, and
tests without pulling in a live engine.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TransactionRecord(Base):
    """Durable, queryable copy of every transaction that passed schema
    validation. This is the system of record for compliance/audit and for
    offline model training in later phases."""

    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    card_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    merchant_category: Mapped[str] = mapped_column(String(32), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    is_simulated_fraud: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_transactions_card_event_time", "card_id", "event_time"),
    )


class AuditLogRecord(Base):
    """Append-only log of pipeline-level events (validation failures,
    late/out-of-order data, feature-store write failures, etc.) used for
    debugging and compliance review, independent of the transaction data
    itself."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


# ---------------------------------------------------------------------------
# Phase 2: ML inference audit trail, human-in-the-loop review, and model
# governance tables.
# ---------------------------------------------------------------------------


class PredictionRecord(Base):
    """Immutable audit trail of every `/score` call: exactly which model
    version scored which transaction, with which inputs, and what decision
    resulted. This is both a compliance requirement (explainable, reviewable
    decisions) and the raw material for measuring model drift over time."""

    __tablename__ = "predictions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    card_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ml", doc="'ml' or 'fallback_rules'"
    )
    input_features: Mapped[dict] = mapped_column(JSON, nullable=False)
    fraud_score: Mapped[float] = mapped_column(Float, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, doc="approve|review|block")
    routed_to_review: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (Index("ix_predictions_transaction_id", "transaction_id"),)


class ReviewCaseRecord(Base):
    """Case-management queue: transactions routed to a human analyst because
    the model's confidence was low, the risk level was medium/high, or the
    fallback rules fired. Analysts work this queue in the Streamlit review UI."""

    __tablename__ = "review_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    prediction_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    transaction_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fraud_score: Mapped[float] = mapped_column(Float, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", doc="pending|in_review|resolved"
    )
    assigned_analyst: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalystFeedbackRecord(Base):
    """Ground-truth label an analyst assigns after reviewing a case. This is
    the feedback loop that closes the retraining cycle: false positives and
    false negatives collected here become labeled training data for the
    next model version."""

    __tablename__ = "analyst_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    transaction_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    analyst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        doc="confirmed_fraud|confirmed_legitimate|false_positive|false_negative",
    )
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class ModelApprovalRecord(Base):
    """Governance audit trail for model promotion. A Staging -> Production
    transition is only permitted (see src/ml/registry.py::promote_model)
    if a row exists here for that exact model name/version/target stage --
    i.e. explicit human sign-off is a hard precondition of promotion, not
    just a recommendation."""

    __tablename__ = "model_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    from_stage: Mapped[str] = mapped_column(String(16), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metrics_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (Index("ix_model_approvals_name_version", "model_name", "model_version"),)


# ---------------------------------------------------------------------------
# Phase 3: drift detection audit trail.
# ---------------------------------------------------------------------------


class DriftReportRecord(Base):
    """One row per `dags/drift_detection_dag.py` run: the full KS-test/PSI
    report, queryable for the Grafana model-health dashboard and for
    `automated_promotion_dag.py`'s "was drift the trigger?" decision, and
    durable even though the same numbers are also pushed to Prometheus
    (Pushgateway metrics don't retain history the way a table does)."""

    __tablename__ = "drift_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reference_window: Mapped[str] = mapped_column(String(64), nullable=False)
    current_window: Mapped[str] = mapped_column(String(64), nullable=False)
    any_drift_detected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    drifted_features: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    max_psi: Mapped[float] = mapped_column(Float, nullable=False)
    score_ks_p_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_psi: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_drifted: Mapped[bool] = mapped_column(Boolean, default=False)
    full_report: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
