"""Security headers and request-context middleware."""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# This is a JSON API and the admin dashboard is a separate origin, so the
# policy can be maximally restrictive: no scripts, no framing, no plugins.
CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

BASE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in BASE_HEADERS.items():
            response.headers.setdefault(header, value)
        if self._settings.is_production:
            # Only meaningful over TLS, and harmful if set during local http dev.
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id to the logging context and emit one access log line.

    The log records method, path template and status — never the query string
    or body, which can contain caller-supplied detail.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("http.request_failed", method=request.method, path=request.url.path)
            structlog.contextvars.clear_contextvars()
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        structlog.contextvars.clear_contextvars()
        return response
