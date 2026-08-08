"""POST /score -- real-time fraud scoring."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_model_state, get_postgres_client
from src.api.inference import ModelState, score_transaction
from src.api.schemas import ScoreResponse
from src.common.schemas import Transaction
from src.monitoring.metrics import SCORE_LATENCY, SCORE_REQUESTS
from src.storage.postgres_client import PostgresClient

router = APIRouter(tags=["scoring"])


@router.post("/score", response_model=ScoreResponse)
def score(
    txn: Transaction,
    model_state: ModelState = Depends(get_model_state),
    pg_client: PostgresClient = Depends(get_postgres_client),
) -> ScoreResponse:
    with SCORE_LATENCY.time():
        result = score_transaction(model_state, txn)
    SCORE_REQUESTS.labels(model_source=result.model_source, decision=result.routing.decision).inc()

    prediction_id = pg_client.write_prediction(
        transaction_id=txn.transaction_id,
        card_id=txn.card_id,
        model_name=result.model_name,
        model_version=result.model_version,
        input_features=result.feature_row.iloc[0].to_dict(),
        fraud_score=result.score,
        risk_level=result.routing.risk_level,
        decision=result.routing.decision,
        latency_ms=result.latency_ms,
        model_source=result.model_source,
        routed_to_review=result.routing.routed_to_review,
    )

    review_case_id = None
    if result.routing.routed_to_review:
        review_case_id = pg_client.create_review_case(
            prediction_id=prediction_id,
            transaction_id=txn.transaction_id,
            fraud_score=result.score,
            risk_level=result.routing.risk_level,
            reason=result.fallback_reason or result.routing.reason,
        )

    return ScoreResponse(
        transaction_id=txn.transaction_id,
        card_id=txn.card_id,
        fraud_score=round(result.score, 6),
        risk_level=result.routing.risk_level,
        decision=result.routing.decision,
        model_source=result.model_source,
        model_name=result.model_name,
        model_version=str(result.model_version),
        routed_to_review=result.routing.routed_to_review,
        review_case_id=review_case_id,
        latency_ms=round(result.latency_ms, 3),
        prediction_id=prediction_id,
    )
