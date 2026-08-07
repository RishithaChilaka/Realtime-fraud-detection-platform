"""
POST /batch/score -- upload a CSV of transactions, get back the exact
payload the /dashboard frontend renders (KPIs, a timeseries, geo points,
a fraud-reason breakdown, the top flagged transactions, and the current
model's last-validated metrics).

GET /batch/template -- a ready-to-edit sample CSV (generated with the same
`TransactionGenerator` the Kafka producer uses, so it includes realistic
edge cases) matching exactly the columns `/batch/score` expects.

Nothing here writes to PostgreSQL or Redis -- see `src/api/batch.py`'s
module docstring for why an upload is scored as a one-off, throwaway
analysis rather than treated like live traffic.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from src.api.batch import MAX_BATCH_ROWS, build_dashboard_payload, parse_transactions_csv, score_batch
from src.api.dependencies import get_model_state
from src.api.inference import ModelState
from src.common.config import Settings, get_settings
from src.common.logging_config import configure_logging
from src.ingestion.transaction_generator import TransactionGenerator
from src.ml import registry

logger = configure_logging("batch_api")

router = APIRouter(prefix="/batch", tags=["batch"])

_CSV_COLUMNS = [
    "transaction_id", "card_id", "user_id", "amount", "currency", "merchant_id",
    "merchant_category", "transaction_type", "channel", "latitude", "longitude",
    "country", "device_id", "ip_address", "event_time",
]


@router.post("/score")
async def batch_score(
    file: UploadFile,
    model_state: ModelState = Depends(get_model_state),
    settings: Settings = Depends(get_settings),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload a .csv file")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    try:
        transactions, row_errors, rows_in_file = parse_transactions_csv(content)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not transactions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid rows to score -- every row failed validation. "
            "See the response of GET /batch/template for the expected format.",
        )

    truncated = len(transactions) > MAX_BATCH_ROWS
    if truncated:
        transactions = sorted(transactions, key=lambda t: t.event_time)[:MAX_BATCH_ROWS]

    scored = score_batch(model_state, settings, transactions)

    model_metrics = None
    if model_state.is_model_available and model_state.run_id:
        try:
            model_metrics = registry.get_run_metrics(settings, model_state.run_id)
        except Exception as exc:  # MLflow unreachable -- degrade to "unavailable", don't fail the upload
            logger.warning("model_metrics_fetch_failed", error=str(exc))

    payload = build_dashboard_payload(
        state=model_state,
        settings=settings,
        scored=scored,
        row_errors=row_errors,
        rows_in_file=rows_in_file,
        truncated=truncated,
        model_metrics=model_metrics,
    )
    logger.info(
        "batch_scored",
        rows_in_file=rows_in_file,
        rows_scored=len(scored),
        rows_skipped=len(row_errors),
        truncated=truncated,
    )
    return payload


@router.get("/template")
def batch_template(rows: int = 200) -> StreamingResponse:
    """A realistic sample CSV, in the exact shape /batch/score expects --
    generated with a fixed seed so repeated downloads are identical, and
    including the same edge cases (velocity abuse, impossible travel,
    high-value, new-device) the Kafka producer injects, so an upload
    against it actually exercises every row of the dashboard."""
    rows = max(10, min(rows, 5000))
    generator = TransactionGenerator(num_cardholders=max(20, rows // 10), edge_case_ratio=0.12, seed=42)
    start = datetime.now(timezone.utc) - timedelta(hours=6)

    records = [t.model_dump(mode="json") for t in generator.stream(rows, start_time=start)]
    df = pd.DataFrame(records)[_CSV_COLUMNS]

    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample_transactions.csv"},
    )
