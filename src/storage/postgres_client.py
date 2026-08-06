"""
PostgreSQL access layer.

Wraps engine/session creation and exposes small, purpose-built write
functions (`persist_transactions`, `write_audit_log`) instead of leaking
SQLAlchemy Session objects into calling code. This keeps the storage
concern isolated (Single Responsibility) and easy to mock in tests.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from src.common.config import Settings, get_settings
from src.common.logging_config import configure_logging
from src.common.schemas import Transaction
from src.storage.postgres_models import AuditLogRecord, Base, TransactionRecord

logger = configure_logging("postgres_client")


class PostgresClient:
    def __init__(self, settings: Optional[Settings] = None, echo: bool = False) -> None:
        self.settings = settings or get_settings()
        self.engine = create_engine(self.settings.postgres_dsn, echo=echo, pool_pre_ping=True)
        self._SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create tables if they don't exist. Production deployments should
        prefer Alembic migrations (see `alembic/`); this is a convenience
        for local dev and tests."""
        Base.metadata.create_all(self.engine)

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
