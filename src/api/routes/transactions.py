"""GET /transactions/{transaction_id} -- fetch a previously persisted
transaction. Exists so the review UI can re-hydrate a flagged case's full
transaction payload (the review queue only stores the score, not the raw
event) in order to call `/explain` on it."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_postgres_client
from src.common.schemas import Transaction
from src.storage.postgres_client import PostgresClient

router = APIRouter(tags=["transactions"])


@router.get("/transactions/{transaction_id}", response_model=Transaction)
def get_transaction(
    transaction_id: str,
    pg_client: PostgresClient = Depends(get_postgres_client),
) -> Transaction:
    row = pg_client.get_transaction(transaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Transaction {transaction_id} not found")
    return Transaction.model_validate(row)
