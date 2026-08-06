from unittest.mock import MagicMock, patch

import pytest

from src.common.config import Settings
from src.ingestion.producer import TransactionProducer

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_kafka_producer():
    with patch("src.ingestion.producer.Producer") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


class TestTransactionProducer:
    def test_publish_calls_produce_with_card_id_key(self, mock_kafka_producer, make_transaction):
        producer = TransactionProducer(settings=Settings())
        txn = make_transaction(card_id="card_abc")

        producer.publish(txn)

        assert mock_kafka_producer.produce.called
        _, kwargs = mock_kafka_producer.produce.call_args
        assert kwargs["key"] == b"card_abc"
        assert kwargs["topic"] == Settings().kafka_topic_transactions

    def test_flush_delegates_to_underlying_producer(self, mock_kafka_producer):
        producer = TransactionProducer(settings=Settings())
        producer.flush(timeout=5.0)
        mock_kafka_producer.flush.assert_called_once_with(5.0)
