"""
Automated model promotion DAG.

`schedule=None` -- this DAG only ever runs when triggered by
`drift_detection_dag.py` after a drift-triggered retrain completes (see
that DAG's `trigger_promotion_check` task), never on its own clock. It has
no business running unless drift was just detected and a fresh Staging
candidate exists to evaluate.

All the actual policy logic (is automated promotion enabled at all, does
the candidate clear the validation floor, was this really triggered by
drift) lives in `src/ml/auto_promote.py::maybe_auto_promote` -- this DAG
is a thin scheduling wrapper around it, evaluated independently for each
registered model.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/opt/airflow/app")  # see docker/airflow/Dockerfile

from airflow.decorators import dag, task  # noqa: E402

default_args = {
    "owner": "ml-platform",
    "retries": 0,  # promotion is not something to blindly retry
}


@dag(
    dag_id="automated_promotion",
    description="Validation-gated auto-promotion, triggered only by drift_detection after a retrain",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["ml", "governance"],
)
def automated_promotion():
    @task
    def evaluate_and_promote(model_name: str) -> dict:
        from src.common.config import get_settings
        from src.common.logging_config import configure_logging
        from src.ml.auto_promote import maybe_auto_promote
        from src.storage.postgres_client import PostgresClient

        logger = configure_logging("dag.automated_promotion")
        settings = get_settings()
        pg_client = PostgresClient(settings)

        drift_report = pg_client.get_latest_drift_report(model_name)
        result = maybe_auto_promote(settings, model_name, drift_report, pg_client=pg_client)

        logger.info("auto_promotion_evaluated", model_name=model_name, **result)
        return result

    evaluate_and_promote.expand(model_name=["fraud_xgboost", "fraud_lightgbm"])


automated_promotion()
