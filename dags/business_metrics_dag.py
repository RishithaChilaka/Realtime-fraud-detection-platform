"""
Business + model-health metrics export DAG.

Thin wrapper around `src.monitoring.business_metrics.run` (see that module
for why this is a push-based batch job rather than a scraped endpoint).
Runs every 5 minutes so the Grafana business-metrics dashboard and the
review-queue-depth Alertmanager rule stay close to real time.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/opt/airflow/app")  # see docker/airflow/Dockerfile

from airflow.decorators import dag, task  # noqa: E402

default_args = {
    "owner": "ml-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=1),
}


@dag(
    dag_id="business_metrics_export",
    description="Pushes fraud detection rate, review queue depth, FP/FN rate to Prometheus Pushgateway",
    schedule=timedelta(minutes=5),
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["monitoring", "business-metrics"],
)
def business_metrics_export():
    @task
    def export() -> dict:
        from src.common.config import get_settings
        from src.monitoring.business_metrics import run as run_export

        settings = get_settings()
        snapshot = run_export(settings, window_hours=settings.business_metrics_window_hours)
        return snapshot.to_dict()

    export()


business_metrics_export()
