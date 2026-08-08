"""
Row-level validation used inside the Spark streaming job.

Spark's schema (src/common/schemas.py::spark_transaction_schema) enforces
*shape* (types/nullability) when reading JSON off Kafka. This module
enforces *business* validity (the same constraints `Transaction` already
expresses in Pydantic) against a decoded row, so malformed-but-shape-valid
records (e.g. a negative amount, an out-of-range latitude) are routed to
the dead-letter topic/audit log instead of silently polluting features.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from src.common.schemas import Transaction


def validate_row(row: dict[str, Any]) -> tuple[Transaction | None, str | None]:
    """Return (Transaction, None) if `row` is valid, else (None, error_message)."""
    try:
        return Transaction.model_validate(row), None
    except ValidationError as exc:
        return None, exc.json(include_url=False)
