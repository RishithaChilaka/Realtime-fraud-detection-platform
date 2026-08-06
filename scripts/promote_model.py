#!/usr/bin/env python3
"""CLI for the model governance approval workflow: promote a Staging model
version to Production. Requires an explicit `--approved-by`; the approval
is written to the `model_approvals` Postgres audit table before MLflow's
registry stage is touched (see src/ml/registry.py::promote_model)."""
import click

from src.common.config import get_settings
from src.ml.registry import promote_model


@click.command()
@click.option("--model-name", required=True, help="MLflow registered model name, e.g. fraud_xgboost")
@click.option("--version", required=True, help="Model version to promote")
@click.option("--approved-by", required=True, help="Name/id of the approving reviewer")
@click.option("--notes", default=None, help="Approval rationale, logged in the audit trail")
def main(model_name: str, version: str, approved_by: str, notes: str | None) -> None:
    settings = get_settings()
    result = promote_model(
        settings=settings,
        model_name=model_name,
        model_version=version,
        approved_by=approved_by,
        notes=notes,
    )
    click.echo(f"Promoted {model_name} v{version} to {result.current_stage}, approved by {approved_by}")


if __name__ == "__main__":
    main()
