"""Human-in-the-loop review queue: list flagged cases and record analyst
feedback. The Streamlit review UI is a thin client of these endpoints so
every write to the case queue / feedback tables goes through one audited
path, whether it comes from the UI or a script."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_postgres_client
from src.api.schemas import FeedbackRequest, FeedbackResponse, ReviewCase
from src.monitoring.metrics import REVIEW_QUEUE_DEPTH
from src.storage.postgres_client import PostgresClient

router = APIRouter(tags=["review"])


@router.get("/review", response_model=list[ReviewCase])
def list_review_queue(
    status: str = "pending",
    limit: int = 50,
    pg_client: PostgresClient = Depends(get_postgres_client),
) -> list[ReviewCase]:
    cases = pg_client.list_review_cases(status=status, limit=limit)
    if status == "pending":
        REVIEW_QUEUE_DEPTH.set(len(cases))
    return [ReviewCase(**c) for c in cases]


@router.post("/review/{case_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(
    case_id: str,
    feedback: FeedbackRequest,
    pg_client: PostgresClient = Depends(get_postgres_client),
) -> FeedbackResponse:
    case = pg_client.get_review_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Review case {case_id} not found")

    feedback_id = pg_client.submit_feedback(
        case_id=case_id,
        transaction_id=case["transaction_id"],
        analyst_id=feedback.analyst_id,
        label=feedback.label,
        notes=feedback.notes,
    )
    return FeedbackResponse(feedback_id=feedback_id, case_id=case_id, status="resolved")
