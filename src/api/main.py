"""FastAPI application entrypoint.

Startup builds the process-lifetime `ModelState` (loads the Production
model from MLflow if one exists, otherwise the app comes up serving from
the rule-based fallback -- see `inference.ModelState.reload`) and a
`PostgresClient`, both stashed on `app.state` so route handlers can pull
them via `Depends` (see `dependencies.py`) instead of constructing new
connections per request.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from src.api.inference import ModelState
from src.api.routes import explain, health, review, score, transactions
from src.common.config import get_settings
from src.common.logging_config import configure_logging
from src.monitoring.metrics import MODEL_AVAILABLE
from src.storage.postgres_client import PostgresClient

logger = configure_logging("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    pg_client = PostgresClient(settings)
    pg_client.create_all()

    model_state = ModelState(settings)
    MODEL_AVAILABLE.set(1 if model_state.is_model_available else 0)

    app.state.settings = settings
    app.state.pg_client = pg_client
    app.state.model_state = model_state

    logger.info("api_started", model_loaded=model_state.is_model_available)
    yield
    logger.info("api_shutdown")


app = FastAPI(
    title="Fraud Detection Inference API",
    description=(
        "Real-time credit-card fraud scoring: /score for a fraud "
        "probability + risk decision, /explain for SHAP feature "
        "attributions, /review for the analyst case queue."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(score.router)
app.include_router(explain.router)
app.include_router(review.router)
app.include_router(health.router)
app.include_router(transactions.router)

# Prometheus scrape target at /metrics, alongside the JSON API.
app.mount("/metrics", make_asgi_app())
