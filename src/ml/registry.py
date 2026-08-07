"""
MLflow model registry helpers, with a governance gate bolted on.

MLflow's OSS registry tracks stages (`None` -> `Staging` -> `Production` ->
`Archived`) but does not itself enforce any approval workflow -- anyone
with API access can call `transition_model_version_stage` directly. This
module is the *only* supported way this codebase promotes a model to
`Production`, and it refuses to do so unless a matching row already exists
in the `model_approvals` Postgres table (see
`PostgresClient.has_approval`/`record_model_approval`). That is the
"approval workflow requires explicit approval, logged in audit trail"
requirement: the audit trail is the source of truth, MLflow's stage is a
downstream effect of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import mlflow
from mlflow.entities.model_registry import ModelVersion
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from src.common.config import Settings, get_settings
from src.common.logging_config import configure_logging
from src.storage.postgres_client import PostgresClient

logger = configure_logging("ml_registry")


class ApprovalRequiredError(RuntimeError):
    """Raised when a Staging -> Production promotion is attempted without a
    prior recorded approval."""


@dataclass
class LoadedModel:
    model: Any
    model_name: str
    model_version: str
    stage: str
    flavor: str  # "xgboost" | "lightgbm"
    run_id: Optional[str] = None


def _client(settings: Settings) -> MlflowClient:
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    return MlflowClient(tracking_uri=settings.mlflow_tracking_uri)


def get_latest_staging_version(settings: Settings, model_name: str) -> Optional[ModelVersion]:
    """Newest `Staging` version for `model_name`, or `None` if there isn't
    one -- used by `dags/automated_promotion_dag.py` to find the retraining
    DAG's freshly-registered candidate."""
    client = _client(settings)
    versions = client.get_latest_versions(model_name, stages=["Staging"])
    return versions[0] if versions else None


def get_run_metrics(settings: Settings, run_id: str) -> dict[str, float]:
    """Pull back the metrics `train.py` logged for a run (precision,
    recall, fairness_* breakdowns, etc.) -- used to evaluate the automated
    promotion validation gate without re-running evaluation."""
    client = _client(settings)
    run = client.get_run(run_id)
    return dict(run.data.metrics)


def register_and_stage(
    settings: Settings,
    run_id: str,
    model_name: str,
    artifact_path: str,
    target_stage: str = "Staging",
) -> ModelVersion:
    """Register the model artifact from a completed MLflow run and move it
    straight to `target_stage` (default `Staging`). Moving into `Staging`
    is unguarded -- that's the normal "a new candidate is available for
    testing" step. Only `Production` is approval-gated (see `promote_model`)."""
    client = _client(settings)
    model_uri = f"runs:/{run_id}/{artifact_path}"
    result = mlflow.register_model(model_uri=model_uri, name=model_name)
    client.transition_model_version_stage(
        name=model_name,
        version=result.version,
        stage=target_stage,
        archive_existing_versions=False,
    )
    logger.info("model_registered", model_name=model_name, version=result.version, stage=target_stage)
    return client.get_model_version(model_name, result.version)


def promote_model(
    settings: Settings,
    model_name: str,
    model_version: str,
    approved_by: str,
    notes: Optional[str] = None,
    metrics_snapshot: Optional[dict] = None,
    pg_client: Optional[PostgresClient] = None,
) -> ModelVersion:
    """Promote `model_version` of `model_name` to `Production`.

    This is a two-step, order-enforced operation:
      1. Write a `ModelApprovalRecord` to PostgreSQL -- the durable,
         queryable governance audit trail.
      2. Only then call MLflow to actually transition the stage.

    If step 1 fails, step 2 never runs, so MLflow's registry state can
    never advance to Production ahead of the audit trail recording why.
    """
    pg_client = pg_client or PostgresClient(settings)
    client = _client(settings)

    current = client.get_model_version(model_name, model_version)
    if current.current_stage != "Staging":
        logger.warning(
            "promoting_from_unexpected_stage",
            model_name=model_name,
            version=model_version,
            current_stage=current.current_stage,
        )

    pg_client.record_model_approval(
        model_name=model_name,
        model_version=model_version,
        from_stage=current.current_stage,
        to_stage="Production",
        approved_by=approved_by,
        notes=notes,
        metrics_snapshot=metrics_snapshot,
    )

    if not pg_client.has_approval(model_name, model_version, "Production"):
        # Should be unreachable given the write above, but this is the hard
        # gate: no approval row, no promotion, no exceptions.
        raise ApprovalRequiredError(
            f"No recorded approval for {model_name} v{model_version} -> Production"
        )

    client.transition_model_version_stage(
        name=model_name,
        version=model_version,
        stage="Production",
        archive_existing_versions=True,
    )
    logger.info(
        "model_promoted",
        model_name=model_name,
        version=model_version,
        approved_by=approved_by,
    )
    return client.get_model_version(model_name, model_version)


def load_production_model(settings: Optional[Settings] = None) -> Optional[LoadedModel]:
    """Load the current `Production`-stage version of the platform's active
    model (`settings.mlflow_active_model_name`). Returns `None` (rather than
    raising) if MLflow is unreachable or no Production version exists yet --
    callers (the FastAPI service) are expected to fall back to rule-based
    scoring in that case, per the Phase 2 fallback requirement."""
    settings = settings or get_settings()
    try:
        client = _client(settings)
        versions = client.get_latest_versions(settings.mlflow_active_model_name, stages=["Production"])
        if not versions:
            logger.warning("no_production_model", model_name=settings.mlflow_active_model_name)
            return None
        version = versions[0]
        model_uri = f"models:/{settings.mlflow_active_model_name}/Production"
        flavor = "lightgbm" if "lightgbm" in settings.mlflow_active_model_name else "xgboost"
        # Load with the flavor-specific loader (not generic pyfunc) so the
        # returned object is the real XGBClassifier/LGBMClassifier -- with
        # `.predict_proba()` and a usable `.booster_`/tree structure for
        # SHAP's TreeExplainer, neither of which the generic pyfunc wrapper
        # exposes.
        if flavor == "lightgbm":
            model = mlflow.lightgbm.load_model(model_uri)
        else:
            model = mlflow.xgboost.load_model(model_uri)
        return LoadedModel(
            model=model,
            model_name=settings.mlflow_active_model_name,
            model_version=version.version,
            stage="Production",
            flavor=flavor,
            run_id=version.run_id,
        )
    except (MlflowException, OSError) as exc:
        logger.error("model_load_failed", error=str(exc))
        return None
