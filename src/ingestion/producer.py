"""
Kafka producer: validates each simulated transaction against the
`Transaction` Pydantic schema and publishes valid ones to
`kafka_topic_transactions`, keyed by `card_id` so all events for a card
land on the same partition (required for correct per-card ordering, which
the Spark velocity/window features depend on).

Invalid events are never expected from `TransactionGenerator` (it only
builds valid `Transaction` instances) but the validate-then-publish step
is kept explicit so this producer is safe to reuse with any upstream
source, including a future real transaction feed.
"""
from __future__ import annotations

import json
import signal
import time
from typing import Optional

from confluent_kafka import Producer
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from src.common.config import Settings, get_settings
from src.common.logging_config import configure_logging
from src.common.schemas import Transaction
from src.ingestion.transaction_generator import TransactionGenerator
from src.monitoring.metrics import PRODUCER_ERRORS, PRODUCER_LATENCY, PRODUCER_MESSAGES

logger = configure_logging("producer")


def _serialize(txn: Transaction) -> bytes:
    payload = json.loads(txn.model_dump_json())
    return json.dumps(payload).encode("utf-8")


class TransactionProducer:
    """Thin, testable wrapper around confluent_kafka.Producer."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._producer = Producer(
            {
                "bootstrap.servers": self.settings.kafka_bootstrap_servers,
                "enable.idempotence": True,
                "acks": "all",
                "linger.ms": 5,
                "compression.type": "lz4",
            }
        )

    def _delivery_callback(self, err, msg) -> None:
        if err is not None:
            PRODUCER_ERRORS.inc()
            logger.error("delivery_failed", error=str(err), topic=msg.topic())
        else:
            PRODUCER_MESSAGES.inc()

    @retry(wait=wait_exponential(multiplier=0.5, max=5), stop=stop_after_attempt(3))
    def publish(self, txn: Transaction) -> None:
        start = time.perf_counter()
        try:
            self._producer.produce(
                topic=self.settings.kafka_topic_transactions,
                key=txn.card_id.encode("utf-8"),
                value=_serialize(txn),
                callback=self._delivery_callback,
            )
            self._producer.poll(0)
        finally:
            PRODUCER_LATENCY.observe(time.perf_counter() - start)

    def flush(self, timeout: float = 10.0) -> None:
        self._producer.flush(timeout)


def run(tps: Optional[int] = None, edge_case_ratio: Optional[float] = None) -> None:
    """Continuously generate and publish transactions at roughly `tps`
    (transactions per second) until interrupted."""
    settings = get_settings()
    tps = tps or settings.producer_tps
    ratio = edge_case_ratio if edge_case_ratio is not None else settings.producer_edge_case_ratio

    generator = TransactionGenerator(edge_case_ratio=ratio)
    producer = TransactionProducer(settings)

    stop = False

    def _handle_sigterm(*_args) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    logger.info("producer_started", tps=tps, edge_case_ratio=ratio, topic=settings.kafka_topic_transactions)

    batch_interval = 1.0
    while not stop:
        batch_start = time.perf_counter()
        for txn in generator.stream(tps):
            try:
                producer.publish(txn)
            except ValidationError as exc:
                logger.error("schema_validation_failed", error=str(exc))
            if stop:
                break
        elapsed = time.perf_counter() - batch_start
        remaining = batch_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    producer.flush()
    logger.info("producer_stopped")


if __name__ == "__main__":
    run()
