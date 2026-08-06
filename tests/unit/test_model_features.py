from datetime import datetime, timezone

import pytest

from src.common.schemas import TransactionChannel, TransactionType
from src.feature_engineering.features import compute_features
from src.ml.model_features import (
    CATEGORICAL_COLUMNS,
    FEATURE_COLUMNS,
    build_feature_row,
    rows_to_dataframe,
    select_model_matrix,
)

pytestmark = pytest.mark.unit


class TestBuildFeatureRow:
    def test_row_has_all_expected_columns(self, make_transaction):
        txn = make_transaction(event_time=datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc))
        fv = compute_features(history=[], current=txn)

        row = build_feature_row(fv, txn)

        assert set(row.keys()) == set(FEATURE_COLUMNS)

    def test_one_hot_channel_columns_sum_to_one(self, make_transaction):
        txn = make_transaction(channel=TransactionChannel.MOBILE)
        fv = compute_features(history=[], current=txn)

        row = build_feature_row(fv, txn)

        channel_cols = [c for c in CATEGORICAL_COLUMNS if c.startswith("channel_")]
        assert sum(row[c] for c in channel_cols) == 1
        assert row["channel_mobile"] == 1

    def test_one_hot_transaction_type_columns_sum_to_one(self, make_transaction):
        txn = make_transaction(transaction_type=TransactionType.REFUND)
        fv = compute_features(history=[], current=txn)

        row = build_feature_row(fv, txn)

        type_cols = [c for c in CATEGORICAL_COLUMNS if c.startswith("txn_type_")]
        assert sum(row[c] for c in type_cols) == 1
        assert row["txn_type_refund"] == 1

    def test_missing_history_features_use_sentinel_fill_values(self, make_transaction):
        txn = make_transaction()
        fv = compute_features(history=[], current=txn)

        row = build_feature_row(fv, txn)

        assert row["seconds_since_last_txn"] == pytest.approx(999_999.0)
        assert row["distance_from_last_txn_km"] == pytest.approx(0.0)
        assert row["implied_speed_kmh"] == pytest.approx(0.0)

    def test_hour_of_day_and_weekend_derived_from_event_time(self, make_transaction):
        # 2026-08-08 is a Saturday
        txn = make_transaction(event_time=datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc))
        fv = compute_features(history=[], current=txn)

        row = build_feature_row(fv, txn)

        assert row["hour_of_day"] == 15
        assert row["is_weekend"] == 1

    def test_weekday_is_not_flagged_weekend(self, make_transaction):
        # 2026-08-06 is a Thursday
        txn = make_transaction(event_time=datetime(2026, 8, 6, 15, 30, tzinfo=timezone.utc))
        fv = compute_features(history=[], current=txn)

        row = build_feature_row(fv, txn)

        assert row["is_weekend"] == 0


class TestDataFrameHelpers:
    def test_select_model_matrix_returns_columns_in_fixed_order(self, make_transaction):
        txn = make_transaction()
        fv = compute_features(history=[], current=txn)
        row = build_feature_row(fv, txn)
        row["transaction_id"] = txn.transaction_id  # extra bookkeeping column

        df = rows_to_dataframe([row])
        matrix = select_model_matrix(df)

        assert list(matrix.columns) == FEATURE_COLUMNS
        assert "transaction_id" not in matrix.columns
