"""ASGI middleware: request-level Prometheus counters and PII-safe access
logging. Kept separate from route handlers so every endpoint (present and
future) is covered automatically instead of relying on each handler to
remember to instrument itself."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.common.logging_config import configure_logging
from src.common.pii import mask_ip_address
from src.monitoring.metrics import API_REQUESTS

logger = configure_logging("api.access")


class MetricsAndAccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        # Route path template (e.g. "/transactions/{transaction_id}"), not
        # the raw URL, so a metric label can't explode into one series per
        # transaction_id.
        path_template = request.url.path
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            status_class = f"{status_code // 100}xx"
            API_REQUESTS.labels(path=path_template, status_class=status_class).inc()
            logger.info(
                "http_request",
                method=request.method,
                path=path_template,
                status_code=status_code,
                duration_ms=round(elapsed_ms, 2),
                client_ip=mask_ip_address(request.client.host if request.client else None),
            )
