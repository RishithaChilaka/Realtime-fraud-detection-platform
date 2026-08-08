"""GET /health -- liveness/readiness, including whether the API is
currently serving from the ML model or has degraded to rule-based
fallback (surfaced here so this is visible to load balancers/Grafana, not
just buried in logs)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.auth import require_role
from src.api.dependencies import get_model_state
from src.api.inference import ModelState
from src.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(model_state: ModelState = Depends(get_model_state)) -> HealthResponse:
    # Deliberately unauthenticated: load balancer / ECS / k8s health checks
    # need to hit this without a token.
    return HealthResponse(
        status="ok" if model_state.is_model_available else "degraded",
        model_loaded=model_state.is_model_available,
        model_name=model_state.model_name,
        model_version=model_state.model_version,
        fallback_active=not model_state.is_model_available,
    )


@router.post(
    "/admin/reload-model",
    response_model=HealthResponse,
    dependencies=[Depends(require_role("admin"))],
)
def reload_model(model_state: ModelState = Depends(get_model_state)) -> HealthResponse:
    """Re-check the MLflow registry for a newer Production version without
    restarting the process -- called after `scripts/promote_model.py`.
    Admin-only: this changes what every subsequent request is scored by."""
    model_state.reload()
    return health(model_state)
