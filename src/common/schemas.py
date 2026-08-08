"""
Canonical data contracts for the platform.

`Transaction` is the single source of truth for what a valid transaction
looks like. The Kafka producer validates against it before publishing, and
the Spark consumer validates against the equivalent Spark schema
(`spark_transaction_schema`) so both ends of the pipeline agree on shape
and constraints even though they run in different processes/runtimes.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pyspark.sql.types import (
    DoubleType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)


class TransactionType(str, enum.Enum):
    PURCHASE = "purchase"
    WITHDRAWAL = "withdrawal"
    REFUND = "refund"
    TRANSFER = "transfer"


class TransactionChannel(str, enum.Enum):
    ONLINE = "online"
    IN_STORE = "in_store"
    ATM = "atm"
    MOBILE = "mobile"


class Transaction(BaseModel):
    """A single credit-card transaction event as produced onto Kafka."""

    transaction_id: str = Field(..., min_length=1, max_length=64)
    card_id: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(..., min_length=1, max_length=64)
    amount: float = Field(..., gt=0, le=1_000_000)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    merchant_id: str = Field(..., min_length=1, max_length=64)
    merchant_category: str = Field(..., min_length=1, max_length=32)
    transaction_type: TransactionType
    channel: TransactionChannel
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    country: str = Field(..., min_length=2, max_length=2, description="ISO-3166 alpha-2")
    device_id: Optional[str] = Field(default=None, max_length=64)
    ip_address: Optional[str] = Field(default=None, max_length=45)
    event_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_simulated_fraud: bool = Field(
        default=False,
        description="Ground-truth label injected by the simulator for offline eval only; "
        "never available to the real-time scorer.",
    )

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.upper()

    @field_validator("country")
    @classmethod
    def uppercase_country(cls, v: str) -> str:
        return v.upper()

    model_config = {
        "use_enum_values": True,
        "json_schema_extra": {
            "example": {
                "transaction_id": "txn_8f3a1c",
                "card_id": "card_1234",
                "user_id": "user_5678",
                "amount": 42.50,
                "currency": "USD",
                "merchant_id": "merchant_99",
                "merchant_category": "grocery",
                "transaction_type": "purchase",
                "channel": "in_store",
                "latitude": 37.7749,
                "longitude": -122.4194,
                "country": "US",
                "device_id": "device_abc",
                "ip_address": "203.0.113.5",
                "event_time": "2026-08-06T12:00:00Z",
                "is_simulated_fraud": False,
            }
        },
    }


# Spark equivalent of the Pydantic schema above. Kept in the same module so
# a schema change forces the author to update both representations together.
spark_transaction_schema = StructType(
    [
        StructField("transaction_id", StringType(), nullable=False),
        StructField("card_id", StringType(), nullable=False),
        StructField("user_id", StringType(), nullable=False),
        StructField("amount", DoubleType(), nullable=False),
        StructField("currency", StringType(), nullable=False),
        StructField("merchant_id", StringType(), nullable=False),
        StructField("merchant_category", StringType(), nullable=False),
        StructField("transaction_type", StringType(), nullable=False),
        StructField("channel", StringType(), nullable=False),
        StructField("latitude", DoubleType(), nullable=False),
        StructField("longitude", DoubleType(), nullable=False),
        StructField("country", StringType(), nullable=False),
        StructField("device_id", StringType(), nullable=True),
        StructField("ip_address", StringType(), nullable=True),
        StructField("event_time", TimestampType(), nullable=False),
        StructField("is_simulated_fraud", StringType(), nullable=True),
    ]
)

__all__ = [
    "Transaction",
    "TransactionType",
    "TransactionChannel",
    "spark_transaction_schema",
]
