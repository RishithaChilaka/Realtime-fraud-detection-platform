"""
PostgreSQL access layer.

Wraps engine/session creation and exposes small, purpose-built write
functions (`persist_transactions`, `write_audit_log`) instead of leaking
SQLAlchemy Session objects into calling code. This keeps the storage
concern isolated (Single Responsibility) and easy to mock in tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from src.common.config import Settings, get_settings
from src.common.logging_config import configure_logging
from src.common.schemas import Transaction
from src.storage.postgres_models import (
    AnalystFeedbackRecord,
    AuditLogRecord,
    Base,
    DriftReportRecord,
    ModelApprovalRecord,
    PredictionRecord,
    ReviewCaseRecord,
    TransactionRecord,
)

logger = configure_logging("postgres_client")


class PostgresClient:
    def __init__(self, settings: Optional[Settings] = None, echo: bool = False) -> None:
        self.settings = settings or get_settings()
        self.engine = create_engine(self.settings.postgres_dsn, echo=echo, pool_pre_ping=True)
        self._SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create tables if they don't exist. Production deployments should
        prefer Alembic migrations (see `alembic/`); this is a convenience
        for local dev and tests.

        Both the api and consumer services call this independently at
        startup, and in Docker Compose the schema is *also* created by
        sql/init.sql (baked into the postgres image, run automatically by
        Postgres on first container init -- see docker/postgres/Dockerfile).
        `create_all`'s checkfirst logic reliably skips tables that already
        exist, but has been observed to still attempt to (re)create an
        index that init.sql already created, which Postgres correctly
        rejects as a duplicate object.

        Creating tables one at a time (rather than a single
        `Base.metadata.create_all(self.engine)` call) matters here: that's
        a single bulk operation from SQLAlchemy's perspective, so if any
        one table/index in the middle raises "already exists", everything
        after it in creation order silently never gets attempted at all --
        caught a real integration test failure where an early "already
        exists" swallowed the same way meant a *later* table never got
        created on an otherwise-empty test database. Per-table try/except
        means one duplicate-object error can't take out the rest of the
        schema. Any other database error still propagates normally."""
        for table in Base.metadata.sorted_tables:
            try:
                table.create(self.engine, checkfirst=True)
            except ProgrammingError as exc:
                if "already exists" in str(exc.orig):
                    logger.debug(
                        "schema_object_already_exists", table=table.name, detail=str(exc.orig)
                    )
                else:
                    raise

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def persist_transactions(self, transactions: Iterable[Transaction]) -> int:
        """Upsert a batch of transactions. Idempotent on `transaction_id` so
        Spark's at-least-once delivery semantics can't create duplicates."""
        rows = [
            {
                "transaction_id": t.transaction_id,
                "card_id": t.card_id,
                "user_id": t.user_id,
                "amount": t.amount,
                "currency": t.currency,
                "merchant_id": t.merchant_id,
                "merchant_category": t.merchant_category,
                "transaction_type": t.transaction_type,
                "channel": t.channel,
                "latitude": t.latitude,
                "longitude": t.longitude,
                "country": t.country,
                "device_id": t.device_id,
                "ip_address": t.ip_address,
                "event_time": t.event_time,
                "is_simulated_fraud": t.is_simulated_fraud,
            }
            for t in transactions
        ]
        if not rows:
            return 0

        with self.session() as session:
            stmt = pg_insert(TransactionRecord).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["transaction_id"])
            result = session.execute(stmt)
            return result.rowcount or 0

    def get_transaction(self, transaction_id: str) -> Optional[dict[str, Any]]:
        """Fetch a previously persisted transaction, reconstructed as a
        plain dict matching the `Transaction` schema's field names. Used by
        the review UI to re-fetch a flagged transaction's full payload (the
        review queue itself only stores the score, not the raw event) so it
        can call `/explain` on it."""
        with self.session() as session:
            record = session.get(TransactionRecord, transaction_id)
            if record is None:
                return None
            return {
                "transaction_id": record.transaction_id,
                "card_id": record.card_id,
                "user_id": record.user_id,
                "amount": record.amount,
                "currency": record.currency,
                "merchant_id": record.merchant_id,
                "merchant_category": record.merchant_category,
                "transaction_type": record.transaction_type,
                "channel": record.channel,
                "latitude": record.latitude,
                "longitude": record.longitude,
                "country": record.country,
                "device_id": record.device_id,
                "ip_address": record.ip_address,
                "event_time": record.event_time.isoformat(),
                "is_simulated_fraud": record.is_simulated_fraud,
            }

    def write_audit_log(
        self,
        event_type: str,
        message: str,
        transaction_id: Optional[str] = None,
        severity: str = "info",
    ) -> None:
        with self.session() as session:
            session.add(
                AuditLogRecord(
                    event_type=event_type,
                    transaction_id=transaction_id,
                    severity=severity,
                    message=message,
                )
            )

    # -- Phase 2: prediction audit trail -----------------------------------

    def write_prediction(
        self,
        transaction_id: str,
        card_id: str,
        model_name: str,
        model_version: str,
        input_features: dict,
        fraud_score: float,
        risk_level: str,
        decision: str,
        latency_ms: float,
        model_source: str = "ml",
        routed_to_review: bool = False,
    ) -> str:
        """Insert one immutable prediction audit row and return its id (used
        to link a review case back to the exact prediction that created it)."""
        record = PredictionRecord(
            transaction_id=transaction_id,
            card_id=card_id,
            model_name=model_name,
            model_version=model_version,
            model_source=model_source,
            input_features=input_features,
            fraud_score=fraud_score,
            risk_level=risk_level,
            decision=decision,
            routed_to_review=routed_to_review,
            latency_ms=latency_ms,
        )
        with self.session() as session:
            session.add(record)
            session.flush()
            return record.id

    # -- Phase 2: human-in-the-loop review queue ----------------------------

    def create_review_case(
        self,
        prediction_id: str,
        transaction_id: str,
        fraud_score: float,
        risk_level: str,
        reason: str,
    ) -> str:
        record = ReviewCaseRecord(
            prediction_id=prediction_id,
            transaction_id=transaction_id,
            fraud_score=fraud_score,
            risk_level=risk_level,
            reason=reason,
        )
        with self.session() as session:
            session.add(record)
            session.flush()
            return record.id

    def list_review_cases(self, status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
        with self.session() as session:
            stmt = (
                select(ReviewCaseRecord)
                .where(ReviewCaseRecord.status == status)
                .order_by(ReviewCaseRecord.created_at.desc())
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [
                {
                    "id": r.id,
                    "prediction_id": r.prediction_id,
                    "transaction_id": r.transaction_id,
                    "fraud_score": r.fraud_score,
                    "risk_level": r.risk_level,
                    "reason": r.reason,
                    "status": r.status,
                    "assigned_analyst": r.assigned_analyst,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]

    def get_review_case(self, case_id: str) -> Optional[dict[str, Any]]:
        with self.session() as session:
            record = session.get(ReviewCaseRecord, case_id)
            if record is None:
                return None
            return {
                "id": record.id,
                "prediction_id": record.prediction_id,
                "transaction_id": record.transaction_id,
                "fraud_score": record.fraud_score,
                "risk_level": record.risk_level,
                "reason": record.reason,
                "status": record.status,
                "assigned_analyst": record.assigned_analyst,
                "created_at": record.created_at.isoformat(),
            }

    def get_prediction(self, prediction_id: str) -> Optional[dict[str, Any]]:
        with self.session() as session:
            record = session.get(PredictionRecord, prediction_id)
            if record is None:
                return None
            return {
                "id": record.id,
                "transaction_id": record.transaction_id,
                "card_id": record.card_id,
                "model_name": record.model_name,
                "model_version": record.model_version,
                "model_source": record.model_source,
                "input_features": record.input_features,
                "fraud_score": record.fraud_score,
                "risk_level": record.risk_level,
                "decision": record.decision,
            }

    def submit_feedback(
        self,
        case_id: str,
        transaction_id: str,
        analyst_id: str,
        label: str,
        notes: Optional[str] = None,
    ) -> str:
        """Record an analyst's ground-truth label and mark the case
        resolved. This is the write path that produces retraining data."""
        with self.session() as session:
            feedback = AnalystFeedbackRecord(
                case_id=case_id,
                transaction_id=transaction_id,
                analyst_id=analyst_id,
                label=label,
                notes=notes,
            )
            session.add(feedback)

            case = session.get(ReviewCaseRecord, case_id)
            if case is not None:
                case.status = "resolved"
                case.assigned_analyst = analyst_id
                case.resolved_at = datetime.now(timezone.utc)

            session.flush()
            return feedback.id

    def list_feedback(self, limit: int = 200) -> list[dict[str, Any]]:
        """All analyst-labeled cases, newest first -- this is the export
        surface a retraining job would pull from."""
        with self.session() as session:
            stmt = (
                select(AnalystFeedbackRecord)
                .order_by(AnalystFeedbackRecord.created_at.desc())
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [
                {
                    "id": r.id,
                    "case_id": r.case_id,
                    "transaction_id": r.transaction_id,
                    "analyst_id": r.analyst_id,
                    "label": r.label,
                    "notes": r.notes,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]

    # -- Phase 2: model governance -------------------------------------------

    def record_model_approval(
        self,
        model_name: str,
        model_version: str,
        from_stage: str,
        to_stage: str,
        approved_by: str,
        notes: Optional[str] = None,
        metrics_snapshot: Optional[dict] = None,
    ) -> str:
        record = ModelApprovalRecord(
            model_name=model_name,
            model_version=model_version,
            from_stage=from_stage,
            to_stage=to_stage,
            approved_by=approved_by,
            notes=notes,
            metrics_snapshot=metrics_snapshot,
        )
        with self.session() as session:
            session.add(record)
            session.flush()
            return record.id

    # -- Phase 3: drift detection + business/model metrics ------------------

    def record_drift_report(self, model_name: str, report: dict) -> str:
        """Persist a `drift.DriftReport.to_dict()` result. Called by
        `dags/drift_detection_dag.py` after every run."""
        record = DriftReportRecord(
            model_name=model_name,
            reference_window=report["reference_window"],
            current_window=report["current_window"],
            any_drift_detected=report["any_drift_detected"],
            drifted_features=report["drifted_features"],
            max_psi=report["max_psi"],
            score_ks_p_value=report.get("score_ks_p_value"),
            score_psi=report.get("score_psi"),
            score_drifted=report.get("score_drifted", False),
            full_report=report,
        )
        with self.session() as session:
            session.add(record)
            session.flush()
            return record.id

    def get_latest_drift_report(self, model_name: str) -> Optional[dict[str, Any]]:
        with self.session() as session:
            stmt = (
                select(DriftReportRecord)
                .where(DriftReportRecord.model_name == model_name)
                .order_by(DriftReportRecord.created_at.desc())
                .limit(1)
            )
            record = session.execute(stmt).scalars().first()
            if record is None:
                return None
            return record.full_report

    def fetch_predictions_since(
        self, since: datetime, model_name: Optional[str] = None, limit: int = 50_000
    ) -> list[dict[str, Any]]:
        """Recent scored transactions -- the "current window" input for
        drift detection, and the base population for business-metric
        aggregation (fraud detection rate, review queue size trend)."""
        with self.session() as session:
            stmt = select(PredictionRecord).where(PredictionRecord.created_at >= since)
            if model_name:
                stmt = stmt.where(PredictionRecord.model_name == model_name)
            stmt = stmt.order_by(PredictionRecord.created_at.desc()).limit(limit)
            rows = session.execute(stmt).scalars().all()
            return [
                {
                    "id": r.id,
                    "transaction_id": r.transaction_id,
                    "card_id": r.card_id,
                    "model_name": r.model_name,
                    "model_version": r.model_version,
                    "model_source": r.model_source,
                    "input_features": r.input_features,
                    "fraud_score": r.fraud_score,
                    "risk_level": r.risk_level,
                    "decision": r.decision,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    def fetch_feedback_with_predictions(
        self, since: datetime, limit: int = 50_000
    ) -> list[dict[str, Any]]:
        """Join `analyst_feedback` back to the `predictions` row it
        resolves, for false-positive/false-negative rate computation:
        a `false_positive` label means the model/decision flagged a
        transaction the analyst confirmed was legitimate; `false_negative`
        means a transaction that should have been flagged wasn't."""
        with self.session() as session:
            stmt = (
                select(AnalystFeedbackRecord, PredictionRecord)
                .join(
                    ReviewCaseRecord,
                    AnalystFeedbackRecord.case_id == ReviewCaseRecord.id,
                )
                .join(
                    PredictionRecord,
                    ReviewCaseRecord.prediction_id == PredictionRecord.id,
                )
                .where(AnalystFeedbackRecord.created_at >= since)
                .limit(limit)
            )
            rows = session.execute(stmt).all()
            return [
                {
                    "label": feedback.label,
                    "decision": prediction.decision,
                    "fraud_score": prediction.fraud_score,
                    "transaction_id": feedback.transaction_id,
                }
                for feedback, prediction in rows
            ]

    def fetch_labeled_training_examples(
        self, since: datetime, limit: int = 50_000
    ) -> list[dict[str, Any]]:
        """Analyst-confirmed ground truth, ready for retraining: joins
        `analyst_feedback` back to the exact `input_features` row
        `PredictionRecord` stored at scoring time (no need to recompute
        features from raw history after the fact -- the feature vector
        used to make the original decision is already durable), and maps
        the analyst's QA label to a binary fraud label:
          confirmed_fraud, false_negative  -> 1 (is fraud)
          confirmed_legitimate, false_positive -> 0 (not fraud)
        """
        with self.session() as session:
            stmt = (
                select(AnalystFeedbackRecord, PredictionRecord)
                .join(ReviewCaseRecord, AnalystFeedbackRecord.case_id == ReviewCaseRecord.id)
                .join(PredictionRecord, ReviewCaseRecord.prediction_id == PredictionRecord.id)
                .where(AnalystFeedbackRecord.created_at >= since)
                .limit(limit)
            )
            rows = session.execute(stmt).all()

        label_map = {
            "confirmed_fraud": 1,
            "false_negative": 1,
            "confirmed_legitimate": 0,
            "false_positive": 0,
        }
        examples = []
        for feedback, prediction in rows:
            if feedback.label not in label_map:
                continue
            examples.append(
                {
                    "transaction_id": feedback.transaction_id,
                    "input_features": prediction.input_features,
                    "label": label_map[feedback.label],
                    "analyst_label": feedback.label,
                }
            )
        return examples

    def count_review_cases(self, status: str = "pending") -> int:
        with self.session() as session:
            stmt = select(ReviewCaseRecord).where(ReviewCaseRecord.status == status)
            return len(session.execute(stmt).scalars().all())

    def has_approval(self, model_name: str, model_version: str, to_stage: str) -> bool:
        """Gate used by `src.ml.registry.promote_model`: a stage transition
        is only allowed if an explicit approval row already exists for this
        exact (model_name, model_version, to_stage) tuple."""
        with self.session() as session:
            stmt = select(ModelApprovalRecord).where(
                ModelApprovalRecord.model_name == model_name,
                ModelApprovalRecord.model_version == model_version,
                ModelApprovalRecord.to_stage == to_stage,
            )
            return session.execute(stmt).first() is not None
