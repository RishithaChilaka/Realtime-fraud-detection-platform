import pytest

from src.common.schemas import Transaction
from src.ingestion.transaction_generator import TransactionGenerator

pytestmark = pytest.mark.unit


class TestTransactionGenerator:
    def test_stream_yields_valid_transactions(self):
        gen = TransactionGenerator(num_cardholders=20, edge_case_ratio=0.0, seed=42)
        txns = list(gen.stream(50))

        assert len(txns) >= 50
        assert all(isinstance(t, Transaction) for t in txns)

    def test_deterministic_with_seed(self):
        gen1 = TransactionGenerator(num_cardholders=10, edge_case_ratio=0.1, seed=123)
        gen2 = TransactionGenerator(num_cardholders=10, edge_case_ratio=0.1, seed=123)

        txns1 = [t.transaction_id for t in gen1.stream(30)]
        txns2 = [t.transaction_id for t in gen2.stream(30)]

        assert txns1 == txns2

    def test_edge_case_ratio_zero_produces_no_labeled_fraud(self):
        gen = TransactionGenerator(num_cardholders=20, edge_case_ratio=0.0, seed=7)
        txns = list(gen.stream(200))

        assert all(t.is_simulated_fraud is False for t in txns)

    def test_high_edge_case_ratio_produces_some_labeled_fraud(self):
        gen = TransactionGenerator(num_cardholders=20, edge_case_ratio=0.9, seed=7)
        txns = list(gen.stream(200))

        assert any(t.is_simulated_fraud is True for t in txns)

    def test_amounts_are_positive(self):
        gen = TransactionGenerator(num_cardholders=20, edge_case_ratio=0.2, seed=1)
        txns = list(gen.stream(100))

        assert all(t.amount > 0 for t in txns)
