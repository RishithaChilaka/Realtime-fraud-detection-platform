"""
Feature vectorization: `FeatureVector` + `Transaction` -> a flat, numeric
row a scikit-learn-compatible model can consume.

This is deliberately the *only* place that knows how to turn engineered
features into a model input row, and it is imported by both training
(`src/ml/dataset.py`) and serving (`src/api/inference.py`). That shared
seam is what prevents train/serve skew -- if this function changes, both
paths pick it up automatically instead of two hand-maintained
implementations drifting apart.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.common.schemas import Transaction, TransactionChannel, TransactionType
from src.feature_engineering.features import FeatureVector

NUMERIC_FEATURES: list[str] = [
    "amount",
    "hour_of_day",
    "is_weekend",
    "txn_count_1h",
    "txn_count_24h",
    "avg_amount_1h",
    "avg_amount_24h",
    "sum_amount_1h",
    "sum_amount_24h",
    "distinct_merchants_24h",
    "velocity_5min",
    "seconds_since_last_txn",
    "distance_from_last_txn_km",
    "implied_speed_kmh",
    "amount_zscore_24h",
    "is_new_device",
]

# Fill values for features that are legitimately absent on a cardholder's
# very first transaction (no prior history to compute them from). Large
# sentinels for "time/distance since last txn" keep a first transaction
# from looking artificially suspicious (zero seconds since last txn would
# look like velocity abuse) or artificially safe (zero distance would look
# like "same place as last time").
_FILL_VALUES: dict[str, float] = {
    "seconds_since_last_txn": 999_999.0,
    "distance_from_last_txn_km": 0.0,
    "implied_speed_kmh": 0.0,
}

_CHANNEL_VALUES = [c.value for c in TransactionChannel]
_TXN_TYPE_VALUES = [t.value for t in TransactionType]

CATEGORICAL_COLUMNS: list[str] = [f"channel_{c}" for c in _CHANNEL_VALUES] + [
    f"txn_type_{t}" for t in _TXN_TYPE_VALUES
]

FEATURE_COLUMNS: list[str] = NUMERIC_FEATURES + CATEGORICAL_COLUMNS


def build_feature_row(feature_vector: FeatureVector, txn: Transaction) -> dict[str, Any]:
    """Flatten a `FeatureVector` + its source `Transaction` into one row,
    with fixed column names/order (`FEATURE_COLUMNS`) so every row -- in
    training or at serving time -- has an identical schema."""
    row: dict[str, Any] = {
        "amount": txn.amount,
        "hour_of_day": txn.event_time.hour,
        "is_weekend": int(txn.event_time.weekday() >= 5),
        "txn_count_1h": feature_vector.txn_count_1h,
        "txn_count_24h": feature_vector.txn_count_24h,
        "avg_amount_1h": feature_vector.avg_amount_1h,
        "avg_amount_24h": feature_vector.avg_amount_24h,
        "sum_amount_1h": feature_vector.sum_amount_1h,
        "sum_amount_24h": feature_vector.sum_amount_24h,
        "distinct_merchants_24h": feature_vector.distinct_merchants_24h,
        "velocity_5min": feature_vector.velocity_5min,
        "seconds_since_last_txn": (
            feature_vector.seconds_since_last_txn
            if feature_vector.seconds_since_last_txn is not None
            else _FILL_VALUES["seconds_since_last_txn"]
        ),
        "distance_from_last_txn_km": (
            feature_vector.distance_from_last_txn_km
            if feature_vector.distance_from_last_txn_km is not None
            else _FILL_VALUES["distance_from_last_txn_km"]
        ),
        "implied_speed_kmh": (
            feature_vector.implied_speed_kmh
            if feature_vector.implied_speed_kmh is not None
            else _FILL_VALUES["implied_speed_kmh"]
        ),
        "amount_zscore_24h": feature_vector.amount_zscore_24h,
        "is_new_device": int(feature_vector.is_new_device),
    }
    txn_channel = txn.channel.value if hasattr(txn.channel, "value") else str(txn.channel)
    txn_type = (
        txn.transaction_type.value
        if hasattr(txn.transaction_type, "value")
        else str(txn.transaction_type)
    )
    for c in _CHANNEL_VALUES:
        row[f"channel_{c}"] = int(txn_channel == c)
    for t in _TXN_TYPE_VALUES:
        row[f"txn_type_{t}"] = int(txn_type == t)
    return row


def rows_to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a DataFrame with a fixed, model-ready column order. Any
    accidental extra keys (e.g. `label`, `transaction_id` kept around for
    dataset bookkeeping) pass through untouched; callers select
    `FEATURE_COLUMNS` before handing the frame to a model."""
    return pd.DataFrame(rows)


def select_model_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """The exact, ordered column subset a model expects. Both training and
    the API's inference path call this right before `model.predict(...)`."""
    return df[FEATURE_COLUMNS]
