"""
Daily batch retraining DAG.

Runs `src.ml.train.run`, which now blends two data sources every time
(see `src/ml/dataset.py::build_feedback_dataset`):
  - fresh synthetic transactions from `TransactionGenerator` (Phase 1),
    regenerated each run rather than cached, so the reference distribution
    drift is measured against keeps moving with any generator changes;
  - analyst-confirmed labels from the human-in-the-loop feedback loop
    (Phase 2's `analyst_feedback` table), when at least
    `min_examples` (default 20) are available in the lookback window.

Both XGBoost and LightGBM are retrained and registered as new `Staging`
versions -- this DAG never promotes anything to `Production` itself; see
`automated_promotion_dag.py` for the validation-gated promotion step, and
`scripts/promote_model.py` for the human-approval path.

Runs daily at 02:00 UTC by default, and can also be triggered on-demand by
`drift_detection_dag.py` when it detects drift outside the regular
schedule (`TriggerDagRunOperator`, see that DAG).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/opt/airflow/app")  # see docker/airflow/Dockerfile

from airflow.decorators import dag, task  # noqa: E402
from airflow.models.param import Param  # noqa: E402

default_args = {
    "owner": "ml-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="retrain_pipeline",
    description="Daily batch retraining (synthetic + analyst feedback), registers new Staging versions",
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["ml", "training"],
    params={"reason": Param("scheduled", type="string", description="scheduled | drift_triggered")},
)
def retrain_pipeline():
    @task
    def run_training(**context) -> dict:
        from src.common.config import get_settings
        from src.common.logging_config import configure_logging
        from src.ml.train import run as run_training_pipeline

        logger = configure_logging("dag.retrain_pipeline")
        reason = context["params"].get("reason", "scheduled")
        logger.info("retrain_pipeline_started", reason=reason)

        settings = get_settings()
        result = run_training_pipeline(settings)

        logger.info(
            "retrain_pipeline_finished",
            xgboost_version=result["xgboost"]["version"],
            lightgbm_version=result["lightgbm"]["version"],
            analyst_feedback_rows=result["dataset_summary"]["analyst_feedback_rows"],
        )
        return result

    run_training()


retrain_pipeline()
