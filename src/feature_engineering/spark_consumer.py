"""
Spark Structured Streaming job: Kafka -> validate -> feature engineering -> sinks.

Pipeline per micro-batch (`foreachBatch`):
  1. Read raw JSON off Kafka, parse against `spark_transaction_schema`.
  2. Row-level validation via `validation.validate_row` (Pydantic) -- valid
     rows continue, invalid rows are written to the audit log and skipped.
  3. Per card_id, compute rolling-window features with
     `features.compute_features`, using history pulled from the Redis
     feature store (`RedisFeatureStore.get_history`) plus the batch's own
     transactions sorted by event_time -- this gives correct 1h/24h
     windows and velocity/distance features even across micro-batch
     boundaries.
  4. Write enriched features back to Redis (low-latency serving) and
     persist raw transactions to PostgreSQL (system of record).

Using `foreachBatch` with driver-orchestrated, per-key pandas processing
(rather than a pure `groupBy(window(...))` aggregation) is a deliberate
choice: features like "distance from the previous transaction" and
"seconds since last transaction" are inherently sequential/stateful per
card, which is awkward to express as a single windowed aggregate but is
straightforward once each micro-batch is grouped by card_id and processed
with the shared, unit-tested `features.compute_features` function.
"""

from __future__ import annotations

from typing import Optional

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from src.common.config import Settings, get_settings
from src.common.logging_config import configure_logging
from src.common.schemas import Transaction, spark_transaction_schema
from src.feature_engineering.feature_store import RedisFeatureStore
from src.feature_engineering.features import compute_features
from src.feature_engineering.validation import validate_row
from src.monitoring.metrics import (
    PIPELINE_LAG_SECONDS,
    STREAM_BATCH_LATENCY,
    STREAM_BATCH_RECORDS,
    STREAM_VALIDATION_FAILURES,
)
from src.storage.postgres_client import PostgresClient

logger = configure_logging("spark_consumer")


def build_spark_session(settings: Settings) -> SparkSession:
    return (
        SparkSession.builder.appName(settings.spark_app_name)
        .master(settings.spark_master_url)
        .config(
            "spark.jars.packages",
            "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1",
        )
        .config("spark.sql.shuffle.partitions", "8")
        .config("spark.streaming.stopGracefullyOnShutdown", "true")
        .getOrCreate()
    )


def read_kafka_stream(spark: SparkSession, settings: Settings) -> DataFrame:
    raw = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", settings.kafka_bootstrap_servers)
        .option("subscribe", settings.kafka_topic_transactions)
        .option("startingOffsets", "latest")
        .option("maxOffsetsPerTrigger", "5000")
        .load()
    )
    parsed = raw.select(
        F.from_json(F.col("value").cast("string"), spark_transaction_schema).alias("data")
    ).select("data.*")
    # Watermark bounds how long we wait for late data before a window is
    # considered final -- required for stateful/windowed streaming aggregates.
    return parsed.withWatermark("event_time", "10 minutes")


def _process_partition_group(
    rows: list[dict], store: RedisFeatureStore
) -> tuple[list[Transaction], int]:
    """Validate + compute features for one card_id's rows in a micro-batch,
    seeding history from the feature store so windows are correct across
    batch boundaries. Returns (valid transactions, invalid count)."""
    valid_txns: list[Transaction] = []
    invalid_count = 0

    by_card: dict[str, list[dict]] = {}
    for row in rows:
        by_card.setdefault(row["card_id"], []).append(row)

    for card_id, card_rows in by_card.items():
        history = store.get_history(card_id)
        card_rows.sort(key=lambda r: r["event_time"])
        for row in card_rows:
            txn, error = validate_row(row)
            if txn is None:
                invalid_count += 1
                STREAM_VALIDATION_FAILURES.inc()
                logger.warning("row_validation_failed", card_id=card_id, error=error)
                continue

            feature_vector = compute_features(history, txn)
            store.write_features(feature_vector)
            store.append_history(txn)
            history.append(txn)

            valid_txns.append(txn)

    return valid_txns, invalid_count


def make_foreach_batch_writer(settings: Settings):
    """Returns the function passed to `writeStream.foreachBatch`. Building
    it as a closure over `settings` keeps `process_batch` free of hidden
    globals and easy to unit test by calling it directly with a fake
    DataFrame-like list of Row.asDict() dicts."""

    pg_client = PostgresClient(settings)
    feature_store = RedisFeatureStore(settings)

    def process_batch(batch_df: DataFrame, batch_id: int) -> None:
        import time

        start = time.perf_counter()
        rows = [r.asDict() for r in batch_df.collect()]
        STREAM_BATCH_RECORDS.observe(len(rows))

        if not rows:
            return

        valid_txns, invalid_count = _process_partition_group(rows, feature_store)

        if valid_txns:
            written = pg_client.persist_transactions(valid_txns)
            logger.info(
                "batch_processed",
                batch_id=batch_id,
                total_rows=len(rows),
                valid=len(valid_txns),
                invalid=invalid_count,
                persisted=written,
            )
        if invalid_count:
            pg_client.write_audit_log(
                event_type="validation_failure_batch",
                message=f"{invalid_count} rows failed validation in batch {batch_id}",
                severity="warning",
            )

        elapsed = time.perf_counter() - start
        STREAM_BATCH_LATENCY.observe(elapsed)
        if valid_txns:
            import datetime as _dt

            newest_event = max(t.event_time for t in valid_txns)
            PIPELINE_LAG_SECONDS.set(
                (_dt.datetime.now(_dt.timezone.utc) - newest_event).total_seconds()
            )

    return process_batch


def run(settings: Optional[Settings] = None) -> None:
    settings = settings or get_settings()
    spark = build_spark_session(settings)
    spark.sparkContext.setLogLevel("WARN")

    PostgresClient(settings).create_all()

    stream_df = read_kafka_stream(spark, settings)

    query = (
        stream_df.writeStream.foreachBatch(make_foreach_batch_writer(settings))
        .option("checkpointLocation", settings.spark_checkpoint_dir)
        .trigger(processingTime="1 second")
        .start()
    )
    logger.info("spark_streaming_started", topic=settings.kafka_topic_transactions)
    query.awaitTermination()


if __name__ == "__main__":
    run()
