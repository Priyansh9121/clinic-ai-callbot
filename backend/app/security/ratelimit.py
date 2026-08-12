"""Rate limiting.

A fixed-window counter per client, held in process. Adequate for a single
backend instance; a multi-instance deployment should move the counter to Redis
(the interface below is deliberately small enough to swap).

Booking and conversation endpoints get a tighter limit than reads, because they
are the ones that consume slots and create records.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Path prefix -> (requests, window seconds). Longest prefix wins.
ENDPOINT_LIMITS: dict[str, tuple[int, int]] = {
    "/api/conversation": (60, 60),
    "/api/appointments": (20, 60),
}


class FixedWindowLimiter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._windows: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

    def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Returns ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        with self._lock:
            window_start, count = self._windows[key]
            if now - window_start >= window_seconds:
                self._windows[key] = (now, 1)
                return True, 0
            if count >= limit:
                return False, int(window_seconds - (now - window_start)) + 1
            self._windows[key] = (window_start, count + 1)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


limiter = FixedWindowLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    def _client_key(self, request: Request) -> str:
        # An API key identifies a client more precisely than an IP behind NAT.
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return f"key:{hash(api_key) & 0xFFFFFFFF:08x}"
        client = request.client.host if request.client else "unknown"
        return f"ip:{client}"

    def _limit_for(self, path: str) -> tuple[int, int]:
        best: tuple[int, int] | None = None
        best_len = -1
        for prefix, limits in ENDPOINT_LIMITS.items():
            if path.startswith(prefix) and len(prefix) > best_len:
                best, best_len = limits, len(prefix)
        if best is not None:
            return best
        return self._settings.rate_limit_requests, self._settings.rate_limit_window_seconds

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._settings.rate_limit_enabled or request.url.path.startswith("/health"):
            return await call_next(request)

        limit, window = self._limit_for(request.url.path)
        key = f"{self._client_key(request)}:{request.url.path}"
        allowed, retry_after = limiter.check(key, limit, window)
        if not allowed:
            logger.warning("ratelimit.blocked", path=request.url.path)
            return JSONResponse(
                status_code=429,
                content={
                    "code": "rate_limited",
                    "message": "Too many requests. Please try again shortly.",
                },
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
