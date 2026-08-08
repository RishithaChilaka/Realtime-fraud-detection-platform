import pytest
from pydantic import ValidationError

from src.common.schemas import Transaction

pytestmark = pytest.mark.unit


class TestTransactionValidation:
    def test_valid_transaction_parses(self, make_transaction):
        txn = make_transaction()
        assert txn.amount == 50.0
        assert txn.currency == "USD"

    def test_negative_amount_rejected(self, make_transaction):
        with pytest.raises(ValidationError):
            make_transaction(amount=-10.0)

    def test_zero_amount_rejected(self, make_transaction):
        with pytest.raises(ValidationError):
            make_transaction(amount=0)

    def test_amount_over_max_rejected(self, make_transaction):
        with pytest.raises(ValidationError):
            make_transaction(amount=2_000_000)

    def test_invalid_latitude_rejected(self, make_transaction):
        with pytest.raises(ValidationError):
            make_transaction(latitude=200.0)

    def test_invalid_longitude_rejected(self, make_transaction):
        with pytest.raises(ValidationError):
            make_transaction(longitude=-200.0)

    def test_country_must_be_two_letters(self, make_transaction):
        with pytest.raises(ValidationError):
            make_transaction(country="USA")

    def test_currency_and_country_are_uppercased(self, make_transaction):
        txn = make_transaction(currency="usd", country="us")
        assert txn.currency == "USD"
        assert txn.country == "US"

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            Transaction.model_validate({"amount": 10.0})

    def test_row_dict_round_trip_via_model_validate(self, make_transaction):
        txn = make_transaction()
        row = txn.model_dump(mode="json")
        rebuilt = Transaction.model_validate(row)
        assert rebuilt == txn
