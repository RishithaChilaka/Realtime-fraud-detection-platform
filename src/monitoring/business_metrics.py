"""
Business + model-health metrics exporter.

`/score` and `/explain` are request-scoped -- the metrics in
`src/monitoring/metrics.py` that live inside the FastAPI process (latency
histograms, request counters) are naturally always fresh. Metrics that
require aggregating *across* many past predictions and analyst feedback
(fraud detection rate, false positive/negative rate, review queue size)
don't fit that model -- there's no single request to attach them to.

This module computes those aggregates from PostgreSQL and pushes them to
Prometheus Pushgateway (`docker-compose.yml`'s `pushgateway` service),
the standard pattern for metrics produced by batch/cron jobs rather than
a scraped long-running process. `dags/business_metrics_dag.py` runs this
on a schedule (every 5 minutes); it can also be run ad hoc via
`scripts/export_business_metrics.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

from src.common.config import Settings
from src.common.logging_config import configure_logging
from src.storage.postgres_client import PostgresClient

logger = configure_logging("business_metrics")

_PUSHGATEWAY_JOB = "fraud_business_metrics"


@dataclass
class BusinessMetricsSnapshot:
    window_hours: int
    total_predictions: int
    block_rate: float
    review_rate: float
    approve_rate: float
    fallback_rate: float
    score_p50: float
    score_p95: float
    review_queue_pending: int
    review_queue_in_review: int
    false_positive_rate: float
    false_negative_rate: float
    feedback_sample_size: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def compute_snapshot(pg_client: PostgresClient, window_hours: int = 24) -> BusinessMetricsSnapshot:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    predictions = pg_client.fetch_predictions_since(since)
    feedback = pg_client.fetch_feedback_with_predictions(since)

    total = len(predictions)
    if total > 0:
        decisions = [p["decision"] for p in predictions]
        scores = np.array([p["fraud_score"] for p in predictions])
        block_rate = decisions.count("block") / total
        review_rate = decisions.count("review") / total
        approve_rate = decisions.count("approve") / total
        fallback_rate = sum(1 for p in predictions if p["model_source"] == "fallback_rules") / total
        score_p50 = float(np.percentile(scores, 50))
        score_p95 = float(np.percentile(scores, 95))
    else:
        block_rate = review_rate = approve_rate = fallback_rate = 0.0
        score_p50 = score_p95 = 0.0

    feedback_total = len(feedback)
    if feedback_total > 0:
        labels = [f["label"] for f in feedback]
        false_positive_rate = labels.count("false_positive") / feedback_total
        false_negative_rate = labels.count("false_negative") / feedback_total
    else:
        false_positive_rate = false_negative_rate = 0.0

    return BusinessMetricsSnapshot(
        window_hours=window_hours,
        total_predictions=total,
        block_rate=block_rate,
        review_rate=review_rate,
        approve_rate=approve_rate,
        fallback_rate=fallback_rate,
        score_p50=score_p50,
        score_p95=score_p95,
        review_queue_pending=pg_client.count_review_cases("pending"),
        review_queue_in_review=pg_client.count_review_cases("in_review"),
        false_positive_rate=false_positive_rate,
        false_negative_rate=false_negative_rate,
        feedback_sample_size=feedback_total,
    )


def push_snapshot(settings: Settings, snapshot: BusinessMetricsSnapshot) -> None:
    """Push to Pushgateway using a fresh, private registry -- Pushgateway
    replaces all metrics for this job/instance on every push, so a private
    registry avoids accidentally clobbering unrelated metrics if this ever
    runs in the same process as something else."""
    registry = CollectorRegistry()

    def g(name: str, doc: str) -> Gauge:
        return Gauge(name, doc, registry=registry)

    g("fraud_business_total_predictions", "Predictions in the metrics window").set(
        snapshot.total_predictions
    )
    g("fraud_business_block_rate", "Fraction of predictions decided 'block'").set(
        snapshot.block_rate
    )
    g("fraud_business_review_rate", "Fraction of predictions decided 'review'").set(
        snapshot.review_rate
    )
    g("fraud_business_approve_rate", "Fraction of predictions decided 'approve'").set(
        snapshot.approve_rate
    )
    g(
        "fraud_business_fallback_rate", "Fraction of predictions scored by the rule-based fallback"
    ).set(snapshot.fallback_rate)
    g("fraud_business_score_p50", "Median fraud score in the window").set(snapshot.score_p50)
    g("fraud_business_score_p95", "p95 fraud score in the window").set(snapshot.score_p95)
    g("fraud_review_queue_pending", "Cases awaiting analyst review").set(
        snapshot.review_queue_pending
    )
    g("fraud_review_queue_in_review", "Cases currently being reviewed").set(
        snapshot.review_queue_in_review
    )
    g("fraud_business_false_positive_rate", "Analyst-confirmed false positive rate").set(
        snapshot.false_positive_rate
    )
    g("fraud_business_false_negative_rate", "Analyst-confirmed false negative rate").set(
        snapshot.false_negative_rate
    )
    g("fraud_business_feedback_sample_size", "Analyst feedback rows in the window").set(
        snapshot.feedback_sample_size
    )

    push_to_gateway(settings.pushgateway_url, job=_PUSHGATEWAY_JOB, registry=registry)
    logger.info("business_metrics_pushed", **snapshot.to_dict())


def run(settings: Settings, window_hours: int = 24) -> BusinessMetricsSnapshot:
    pg_client = PostgresClient(settings)
    snapshot = compute_snapshot(pg_client, window_hours=window_hours)
    push_snapshot(settings, snapshot)
    return snapshot
