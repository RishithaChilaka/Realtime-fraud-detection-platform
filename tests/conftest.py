from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.common.schemas import Transaction, TransactionChannel, TransactionType


@pytest.fixture
def make_transaction():
    """Factory fixture: build a valid Transaction with sane defaults,
    overridable per test via kwargs."""

    counter = {"n": 0}

    def _make(**overrides) -> Transaction:
        counter["n"] += 1
        defaults = dict(
            transaction_id=f"txn_test_{counter['n']}",
            card_id="card_test_1",
            user_id="user_test_1",
            amount=50.0,
            currency="USD",
            merchant_id="merchant_1",
            merchant_category="grocery",
            transaction_type=TransactionType.PURCHASE,
            channel=TransactionChannel.IN_STORE,
            latitude=37.7749,
            longitude=-122.4194,
            country="US",
            device_id="device_1",
            ip_address="203.0.113.5",
            event_time=datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc),
            is_simulated_fraud=False,
        )
        defaults.update(overrides)
        return Transaction(**defaults)

    return _make
