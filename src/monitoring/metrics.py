"""
Prometheus metric definitions shared across services.

Kept in one module so `prometheus.yml` scrape configs and Grafana
dashboards can rely on a stable, documented metric name set instead of
metrics being scattered ad-hoc through the codebase.
"""
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# --- Producer metrics ---
PRODUCER_MESSAGES = Counter(
    "fraud_producer_messages_total", "Transactions successfully published to Kafka"
)
PRODUCER_ERRORS = Counter(
    "fraud_producer_errors_total", "Transactions that failed to publish to Kafka"
)
PRODUCER_LATENCY = Histogram(
    "fraud_producer_publish_latency_seconds", "Time to hand a transaction to the Kafka client"
)

# --- Streaming consumer / feature engineering metrics ---
STREAM_BATCH_RECORDS = Histogram(
    "fraud_stream_batch_records", "Number of records processed per micro-batch"
)
STREAM_BATCH_LATENCY = Histogram(
    "fraud_stream_batch_latency_seconds", "End-to-end micro-batch processing time"
)
STREAM_VALIDATION_FAILURES = Counter(
    "fraud_stream_validation_failures_total", "Records that failed schema/business validation"
)
FEATURE_STORE_WRITE_LATENCY = Histogram(
    "fraud_feature_store_write_latency_seconds", "Redis feature-store write latency"
)
FEATURE_STORE_READ_LATENCY = Histogram(
    "fraud_feature_store_read_latency_seconds", "Redis feature-store read latency"
)

# --- Storage metrics ---
POSTGRES_WRITE_LATENCY = Histogram(
    "fraud_postgres_write_latency_seconds", "PostgreSQL batch write latency"
)
PIPELINE_LAG_SECONDS = Gauge(
    "fraud_pipeline_lag_seconds", "Observed lag between event_time and processing_time"
)


def start_metrics_server(port: int) -> None:
    """Expose the process-local Prometheus registry over HTTP for scraping."""
    start_http_server(port)
