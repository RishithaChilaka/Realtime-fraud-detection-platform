"""FastAPI dependency wiring: pulls shared singletons (DB client, model
state) off `app.state` rather than constructing them per-request."""

from __future__ import annotations

from fastapi import Request

from src.api.inference import ModelState
from src.storage.postgres_client import PostgresClient


def get_model_state(request: Request) -> ModelState:
    return request.app.state.model_state


def get_postgres_client(request: Request) -> PostgresClient:
    return request.app.state.pg_client
