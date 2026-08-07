"""FastAPI application entrypoint.

Startup builds the process-lifetime `ModelState` (loads the Production
model from MLflow if one exists, otherwise the app comes up serving from
the rule-based fallback -- see `inference.ModelState.reload`) and a
`PostgresClient`, both stashed on `app.state` so route handlers can pull
them via `Depends` (see `dependencies.py`) instead of constructing new
connections per request.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app

from src.api.inference import ModelState
from src.api.middleware import MetricsAndAccessLogMiddleware
from src.api.routes import auth as auth_routes
from src.api.routes import batch, explain, health, review, score, transactions
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
        "attributions, /review for the analyst case queue.\n\n"
        "Interactive docs (Swagger UI) are auto-generated from this schema "
        "at `/docs`; machine-readable OpenAPI JSON at `/openapi.json`.\n\n"
        "Authentication: `POST /auth/token` with a service/analyst/admin "
        "identity to get a JWT, then `Authorization: Bearer <token>` on "
        "role-protected endpoints (`/review/*`, `/admin/*`) -- see "
        "`src/api/auth.py`. `/score` and `/explain` are unauthenticated by "
        "default (service-to-service calls from the streaming pipeline); "
        "see the README's security section for the production posture.\n\n"
        "A browser dashboard (upload a CSV, see fraud KPIs/charts/alerts "
        "for it) is served at `/dashboard/` -- see `POST /batch/score` and "
        "`GET /batch/template`."
    ),
    version="3.1.0",
    lifespan=lifespan,
)

app.add_middleware(MetricsAndAccessLogMiddleware)

app.include_router(auth_routes.router)
app.include_router(score.router)
app.include_router(explain.router)
app.include_router(review.router)
app.include_router(health.router)
app.include_router(transactions.router)
app.include_router(batch.router)

# Prometheus scrape target at /metrics, alongside the JSON API.
app.mount("/metrics", make_asgi_app())

# The upload-a-file-see-results browser dashboard. Same origin as the API
# (no CORS needed) -- its JS calls `/batch/score` as a relative path.
# `check_dir=False` lets the app boot even before the frontend/ directory
# exists (e.g. a minimal image build), rather than crashing at import time.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_FRONTEND_DIR = os.path.join(_REPO_ROOT, "frontend")
app.mount(
    "/dashboard",
    StaticFiles(directory=_FRONTEND_DIR, html=True, check_dir=False),
    name="dashboard",
)
