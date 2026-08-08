import pytest

from src.feature_engineering.validation import validate_row

pytestmark = pytest.mark.unit


class TestValidateRow:
    def test_valid_row_returns_transaction(self, make_transaction):
        row = make_transaction().model_dump(mode="json")
        txn, error = validate_row(row)

        assert txn is not None
        assert error is None

    def test_invalid_row_returns_error(self, make_transaction):
        row = make_transaction().model_dump(mode="json")
        row["amount"] = -5.0

        txn, error = validate_row(row)

        assert txn is None
        assert error is not None
        assert "amount" in error

    def test_missing_field_returns_error(self):
        txn, error = validate_row({"amount": 10.0})

        assert txn is None
        assert error is not None
