"""
Drift detection DAG: KS test + PSI on feature distributions and on the
prediction-score distribution, for every registered model.

Two different "reference" distributions are used, deliberately, for two
different questions:
  - **Feature drift** ("has live input diverged from what we trained on?")
    compares live `predictions.input_features` against a freshly generated
    sample from the same synthetic distribution `train.py` trains on
    (`build_training_dataset`, fixed seed -- reproducible, no need to
    persist the actual historical training set anywhere).
  - **Score drift** ("has the model's own output distribution shifted over
    time?") compares two adjacent live windows -- the last
    `lookback_hours` of predictions against the `lookback_hours` before
    that -- which needs no access to training-time scores at all and is
    the standard "this week vs last week" pattern for output monitoring.

Runs every 6 hours. When drift is detected for a model, this DAG triggers
`retrain_pipeline` immediately (rather than waiting for its 02:00 UTC
schedule) and, once that completes, triggers `automated_promotion_dag` to
evaluate whether the retrained candidate is eligible to auto-promote.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/opt/airflow/app")  # see docker/airflow/Dockerfile

from airflow.decorators import dag, task  # noqa: E402
from airflow.operators.trigger_dagrun import TriggerDagRunOperator  # noqa: E402
from airflow.utils.trigger_rule import TriggerRule  # noqa: E402

default_args = {
    "owner": "ml-platform",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

LOOKBACK_HOURS = 24
REFERENCE_SAMPLE_SIZE = 5000


@dag(
    dag_id="drift_detection",
    description="KS test + PSI on feature and score distributions; triggers retrain+promotion on drift",
    schedule="0 */6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["ml", "monitoring", "drift"],
)
def drift_detection():
    @task
    def check_model_drift(model_name: str) -> dict:
        from datetime import timezone

        import numpy as np
        import pandas as pd

        from src.common.config import get_settings
        from src.common.logging_config import configure_logging
        from src.ml.dataset import build_training_dataset
        from src.ml.drift import build_drift_report, push_drift_metrics
        from src.ml.model_features import NUMERIC_FEATURES
        from src.storage.postgres_client import PostgresClient

        logger = configure_logging("dag.drift_detection")
        settings = get_settings()
        pg_client = PostgresClient(settings)

        now = datetime.now(timezone.utc)
        current_since = now - timedelta(hours=LOOKBACK_HOURS)
        reference_since = now - timedelta(hours=2 * LOOKBACK_HOURS)

        current_predictions = pg_client.fetch_predictions_since(current_since, model_name=model_name)
        older_predictions = [
            p
            for p in pg_client.fetch_predictions_since(reference_since, model_name=model_name)
            if p["created_at"] < current_since
        ]

        if len(current_predictions) < 30:
            logger.info(
                "insufficient_live_traffic_for_drift_check",
                model_name=model_name,
                current_predictions=len(current_predictions),
            )
            return {"model_name": model_name, "skipped": True, "reason": "insufficient live traffic"}

        current_features_df = pd.DataFrame([p["input_features"] for p in current_predictions])
        current_scores = np.array([p["fraud_score"] for p in current_predictions])

        reference_features_df = build_training_dataset(n_transactions=REFERENCE_SAMPLE_SIZE, seed=42)

        if len(older_predictions) >= 30:
            reference_scores = [p["fraud_score"] for p in older_predictions]
            score_reference_label = f"predictions {reference_since.isoformat()} .. {current_since.isoformat()}"
        else:
            # Not enough live history yet to compare "this window vs last
            # window" for scores -- fall back to skipping score drift for
            # this run rather than comparing against a distribution that
            # isn't actually representative of anything.
            reference_scores = None
            score_reference_label = "insufficient prior live traffic (score drift skipped)"

        report = build_drift_report(
            model_name=model_name,
            reference_df=reference_features_df,
            current_df=current_features_df,
            feature_columns=NUMERIC_FEATURES,
            reference_window=f"synthetic training distribution (n={REFERENCE_SAMPLE_SIZE}, seed=42)",
            current_window=f"live predictions {current_since.isoformat()} .. {now.isoformat()}",
            reference_scores=reference_scores,
            current_scores=current_scores if reference_scores is not None else None,
        )

        report_dict = report.to_dict()
        report_dict["score_reference_label"] = score_reference_label
        pg_client.record_drift_report(model_name, report_dict)
        push_drift_metrics(settings, report)

        logger.info(
            "drift_check_complete",
            model_name=model_name,
            any_drift_detected=report.any_drift_detected,
            drifted_features=report.drifted_features,
            max_psi=round(report.max_psi, 4),
        )
        return {"model_name": model_name, "skipped": False, **report_dict}

    @task.branch
    def decide_next_step(reports: list[dict]) -> str:
        any_drift = any(r.get("any_drift_detected") for r in reports if not r.get("skipped"))
        return "trigger_retrain" if any_drift else "no_drift_detected"

    @task
    def no_drift_detected() -> None:
        from src.common.logging_config import configure_logging

        configure_logging("dag.drift_detection").info("no_drift_detected_this_cycle")

    trigger_retrain = TriggerDagRunOperator(
        task_id="trigger_retrain",
        trigger_dag_id="retrain_pipeline",
        conf={"reason": "drift_triggered"},
        wait_for_completion=True,
        poke_interval=30,
    )

    trigger_promotion_check = TriggerDagRunOperator(
        task_id="trigger_promotion_check",
        trigger_dag_id="automated_promotion",
        wait_for_completion=False,
        trigger_rule=TriggerRule.ALL_SUCCESS,
    )

    reports = check_model_drift.expand(model_name=["fraud_xgboost", "fraud_lightgbm"])
    branch = decide_next_step(reports)
    branch >> [trigger_retrain, no_drift_detected()]
    trigger_retrain >> trigger_promotion_check


drift_detection()
