"""
Automated model promotion: validation gate + audited auto-approval.

The Phase 3 deliverable wants "if drift detected + validation passes ->
deploy new version" with no human in the loop. Phase 2's governance design
(`src/ml/registry.py::promote_model`) hard-requires a `model_approvals`
audit row to exist *before* any Staging -> Production transition. Those
two requirements are reconciled here, not by weakening the gate: automated
promotion still goes through `promote_model`, so it still writes a normal
audit row -- just with `approved_by="airflow-automated-promotion"` instead
of a human name, and `notes` containing the exact evidence (metrics +
drift report) that justified it. Anyone auditing `model_approvals` later
can tell at a glance which promotions were automatic and why.

This is gated three ways, all of which must hold:
  1. `Settings.enable_automated_promotion` is `True` (off by default --
     see config.py for why).
  2. The candidate's logged validation metrics clear the configured floor
     (recall, precision, and a max cross-country fairness recall gap).
  3. A drift report is supplied and indicates drift was actually detected
     (this DAG's whole purpose is "retrain because drift; promote because
     the retrain fixed it" -- promoting a model that wasn't triggered by
     drift isn't this policy's job, that's what a human using
     `scripts/promote_model.py` is for).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mlflow.entities.model_registry import ModelVersion

from src.common.config import Settings
from src.common.logging_config import configure_logging
from src.ml import registry
from src.storage.postgres_client import PostgresClient

logger = configure_logging("ml_auto_promote")


@dataclass
class ValidationResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_validation_gate(settings: Settings, metrics: dict[str, float]) -> ValidationResult:
    reasons: list[str] = []

    recall = metrics.get("recall", 0.0)
    if recall < settings.auto_promotion_min_recall:
        reasons.append(f"recall {recall:.3f} below floor {settings.auto_promotion_min_recall}")

    precision = metrics.get("precision", 0.0)
    if precision < settings.auto_promotion_min_precision:
        reasons.append(f"precision {precision:.3f} below floor {settings.auto_promotion_min_precision}")

    fairness_recalls = {
        key.removeprefix("fairness_").removesuffix("_recall"): value
        for key, value in metrics.items()
        if key.startswith("fairness_") and key.endswith("_recall")
    }
    if len(fairness_recalls) >= 2:
        gap = max(fairness_recalls.values()) - min(fairness_recalls.values())
        if gap > settings.auto_promotion_max_fairness_recall_gap:
            worst = min(fairness_recalls, key=fairness_recalls.get)
            best = max(fairness_recalls, key=fairness_recalls.get)
            reasons.append(
                f"cross-country fairness recall gap {gap:.3f} exceeds "
                f"{settings.auto_promotion_max_fairness_recall_gap} "
                f"(best={best}:{fairness_recalls[best]:.3f}, worst={worst}:{fairness_recalls[worst]:.3f})"
            )

    return ValidationResult(passed=not reasons, reasons=reasons)


def maybe_auto_promote(
    settings: Settings,
    model_name: str,
    drift_report: Optional[dict],
    pg_client: Optional[PostgresClient] = None,
) -> dict:
    """Entry point called by `dags/automated_promotion_dag.py`. Returns a
    structured result dict describing what happened (or why nothing did) --
    the DAG logs this verbatim as its task result."""
    if not settings.enable_automated_promotion:
        return {"promoted": False, "reason": "automated promotion disabled (enable_automated_promotion=False)"}

    if drift_report is None or not drift_report.get("any_drift_detected"):
        return {"promoted": False, "reason": "no drift detected -- automated promotion only triggers on drift"}

    candidate = registry.get_latest_staging_version(settings, model_name)
    if candidate is None:
        return {"promoted": False, "reason": f"no Staging version found for {model_name}"}

    metrics = registry.get_run_metrics(settings, candidate.run_id)
    validation = evaluate_validation_gate(settings, metrics)
    if not validation.passed:
        return {
            "promoted": False,
            "reason": "validation gate failed: " + "; ".join(validation.reasons),
            "candidate_version": candidate.version,
        }

    notes = (
        f"Automated promotion: drift detected (max_psi={drift_report.get('max_psi')}, "
        f"drifted_features={drift_report.get('drifted_features')}); "
        f"validation gate passed (recall={metrics.get('recall'):.3f}, "
        f"precision={metrics.get('precision'):.3f})."
    )

    promoted_version: ModelVersion = registry.promote_model(
        settings=settings,
        model_name=model_name,
        model_version=candidate.version,
        approved_by="airflow-automated-promotion",
        notes=notes,
        metrics_snapshot=metrics,
        pg_client=pg_client,
    )

    logger.info(
        "auto_promoted",
        model_name=model_name,
        version=promoted_version.version,
        drift_max_psi=drift_report.get("max_psi"),
    )
    return {
        "promoted": True,
        "model_name": model_name,
        "version": promoted_version.version,
        "notes": notes,
    }
