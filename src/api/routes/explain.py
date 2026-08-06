"""POST /explain -- top-5 SHAP feature contributions for a transaction.

For transactions scored by the rule-based fallback (no tree model loaded),
SHAP doesn't apply; the response falls back to `explanation_type="rule_based"`
and surfaces which rule(s) fired instead of feature attributions, so the
endpoint degrades gracefully rather than erroring out.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_model_state
from src.api.inference import ModelState, explain_transaction
from src.api.schemas import ExplainResponse
from src.common.schemas import Transaction
from src.monitoring.metrics import EXPLAIN_LATENCY, SHAP_COMPUTE_LATENCY

router = APIRouter(tags=["explainability"])


@router.post("/explain", response_model=ExplainResponse)
def explain(
    txn: Transaction,
    model_state: ModelState = Depends(get_model_state),
) -> ExplainResponse:
    with EXPLAIN_LATENCY.time():
        scoring, shap_explanation = explain_transaction(model_state, txn)

    if shap_explanation is not None:
        SHAP_COMPUTE_LATENCY.observe(shap_explanation.latency_ms / 1000)
        return ExplainResponse(
            transaction_id=txn.transaction_id,
            card_id=txn.card_id,
            explanation_type="shap",
            fraud_score=round(scoring.score, 6),
            base_value=shap_explanation.base_value,
            top_features=shap_explanation.top_features,
            model_name=scoring.model_name,
            model_version=str(scoring.model_version),
            latency_ms=round(scoring.latency_ms + shap_explanation.latency_ms, 3),
        )

    # Fallback path: no tree model available, explain via the rule that fired.
    return ExplainResponse(
        transaction_id=txn.transaction_id,
        card_id=txn.card_id,
        explanation_type="rule_based",
        fraud_score=round(scoring.score, 6),
        base_value=None,
        top_features=[],
        rule_based_reason=scoring.fallback_reason or "no rule triggered",
        model_name=scoring.model_name,
        model_version=str(scoring.model_version),
        latency_ms=round(scoring.latency_ms, 3),
    )
