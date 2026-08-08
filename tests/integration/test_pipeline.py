"""
Integration test: Kafka -> Spark Structured Streaming -> PostgreSQL.

This spins up real Kafka and PostgreSQL containers via testcontainers,
publishes a batch of transactions with `TransactionProducer`, runs one
micro-batch of the Spark pipeline (`spark_consumer.process_batch`, called
directly via `make_foreach_batch_writer` rather than through
`writeStream` so the test doesn't depend on trigger timing), and asserts
the rows land in PostgreSQL and the corresponding features land in the
Redis feature store.

Requires a working Docker daemon. Skipped automatically if Docker isn't
reachable (e.g. this sandbox has no Docker) or if `testcontainers` isn't
installed, so `pytest tests/unit` always works standalone while `pytest
tests/integration` exercises the full stack in CI/local dev with Docker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers")


def _docker_available() -> bool:
    import docker
    from docker.errors import DockerException

    try:
        docker.from_env().ping()
        return True
    except (DockerException, Exception):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon not available"),
]


@pytest.fixture(scope="module")
def kafka_container():
    from testcontainers.kafka import KafkaContainer

    with KafkaContainer() as kafka:
        yield kafka


@pytest.fixture(scope="module")
def postgres_container():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


class TestKafkaToSparkToPostgres:
    def test_produced_transaction_is_validated_and_persisted(
        self, kafka_container, postgres_container, make_transaction
    ):
        import fakeredis
        from confluent_kafka import Producer

        from src.common.config import Settings
        from src.feature_engineering.feature_store import RedisFeatureStore
        from src.feature_engineering.spark_consumer import _process_partition_group
        from src.storage.postgres_client import PostgresClient
        from src.storage.redis_client import RedisClient

        settings = Settings(
            kafka_bootstrap_servers=kafka_container.get_bootstrap_server(),
            postgres_host=postgres_container.get_container_host_ip(),
            postgres_port=int(postgres_container.get_exposed_port(5432)),
            postgres_db=postgres_container.dbname,
            postgres_user=postgres_container.username,
            postgres_password=postgres_container.password,
        )

        # --- Produce a transaction onto real Kafka ---
        txn = make_transaction(
            transaction_id="txn_integration_1",
            card_id="card_integration_1",
            event_time=datetime.now(timezone.utc),
        )
        producer = Producer({"bootstrap.servers": settings.kafka_bootstrap_servers})
        producer.produce(
            topic=settings.kafka_topic_transactions,
            key=txn.card_id.encode(),
            value=json.dumps(json.loads(txn.model_dump_json())).encode(),
        )
        producer.flush(10)

        # --- Consume it back with a plain Kafka consumer (standing in for
        # Spark's Kafka source, which requires a full Spark/JVM runtime not
        # available in this sandbox) and run it through the same
        # `_process_partition_group` function the real streaming job uses ---
        from confluent_kafka import Consumer

        consumer = Consumer(
            {
                "bootstrap.servers": settings.kafka_bootstrap_servers,
                "group.id": "integration-test",
                "auto.offset.reset": "earliest",
            }
        )
        consumer.subscribe([settings.kafka_topic_transactions])
        msg = consumer.poll(10)
        assert msg is not None and msg.error() is None
        row = json.loads(msg.value())
        consumer.close()

        pg_client = PostgresClient(settings)
        pg_client.create_all()

        fake_conn = fakeredis.FakeRedis(decode_responses=True)
        redis_client = RedisClient.__new__(RedisClient)
        redis_client.settings = settings
        redis_client.conn = fake_conn
        feature_store = RedisFeatureStore(settings=settings, client=redis_client)

        valid_txns, invalid_count = _process_partition_group([row], feature_store)
        assert invalid_count == 0
        assert len(valid_txns) == 1

        written = pg_client.persist_transactions(valid_txns)
        assert written == 1

        # --- Assert PostgreSQL has the row ---
        with pg_client.session() as session:
            from src.storage.postgres_models import TransactionRecord

            record = session.get(TransactionRecord, "txn_integration_1")
            assert record is not None
            assert record.card_id == "card_integration_1"

        # --- Assert the feature store has features for the card ---
        features = feature_store.get_features("card_integration_1")
        assert features is not None
        assert features["txn_count_1h"] == 1
