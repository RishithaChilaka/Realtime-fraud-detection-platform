"""
Model card generation.

Renders a governance document from the actual output of a training run
(params, metrics, fairness slice, imbalance-handling technique used)
rather than being hand-written and left to go stale. `train.py` calls
`render_model_card` right after evaluation and both writes the result to
`model_cards/` and logs it as an MLflow artifact on the run, so the model
card a reviewer reads in `model_cards/` and the one attached to the MLflow
run are always the same document.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def render_model_card(
    model_name: str,
    model_version: str,
    algorithm: str,
    imbalance_technique: str,
    training_params: dict[str, Any],
    metrics: dict[str, float],
    fairness_report: dict[str, dict[str, float]],
    dataset_summary: dict[str, Any],
    limitations: list[str],
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()

    metrics_table = "\n".join(f"| {k} | {v:.4f} |" for k, v in metrics.items())

    fairness_table = "\n".join(
        f"| {group} | {stats.get('count', 0):.0f} | {stats.get('recall', 0):.4f} | "
        f"{stats.get('precision', 0):.4f} | {stats.get('positive_rate', 0):.4f} |"
        for group, stats in fairness_report.items()
    )

    params_table = "\n".join(f"| {k} | {v} |" for k, v in training_params.items())

    limitations_list = "\n".join(f"- {item}" for item in limitations)

    return f"""# Model Card: {model_name} (v{model_version})

Generated automatically by `src/ml/train.py` at {generated_at}.

## Model Details

- **Algorithm**: {algorithm}
- **Model name (MLflow registry)**: `{model_name}`
- **Version**: {model_version}
- **Task**: Binary classification -- probability that a credit-card
  transaction is fraudulent, scored in real time at transaction time.
- **Class imbalance handling**: {imbalance_technique}

## Intended Use

Real-time risk scoring for individual credit-card transactions in the
fraud detection platform's `/score` API. Output is a probability in
[0, 1] plus a derived risk level (`low`/`medium`/`high`) and a decision
(`approve`/`review`/`block`) computed from configured thresholds
(`src/common/config.py`). This model is **one input to a decision that
also includes rule-based fallback logic and, for medium/high-risk or
low-confidence cases, mandatory human analyst review** -- it is not
intended to auto-block transactions without that surrounding workflow.

## Training Data

{dataset_summary.get("description", "")}

| | |
|---|---|
| Total transactions | {dataset_summary.get("total_rows", "n/a")} |
| Positive rate (fraud) | {dataset_summary.get("positive_rate", "n/a")} |
| Train / test split | {dataset_summary.get("train_test_split", "n/a")} |
| Cardholder population | {dataset_summary.get("num_cardholders", "n/a")} |
| Feature count | {dataset_summary.get("num_features", "n/a")} |

**Important**: training labels come from `TransactionGenerator`'s injected
edge cases (high-value spikes, impossible travel, velocity bursts,
new-device+high-value -- see Phase 1 `src/ingestion/transaction_generator.py`),
not from analyst-confirmed real-world fraud. See Known Limitations.

## Training Parameters

| Parameter | Value |
|---|---|
{params_table}

## Performance Metrics (held-out test set)

| Metric | Value |
|---|---|
{metrics_table}

## Fairness / Subgroup Evaluation

Recall, precision, and predicted-positive rate broken out by cardholder
home country, as a proxy protected-attribute slice (no demographic data
is collected by this platform). Large gaps between groups would indicate
the model performs unevenly across cardholder populations and should
block promotion pending investigation.

| Country | Support (n) | Recall | Precision | Positive rate |
|---|---|---|---|---|
{fairness_table}

## Known Limitations

{limitations_list}

## Governance

Promotion of this model version from `Staging` to `Production` requires
an explicit, logged approval (`model_approvals` table / `src/ml/registry.py::promote_model`)
-- there is no automatic promotion path. See `scripts/promote_model.py`.
"""
