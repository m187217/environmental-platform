"""FastAPI ASGI middleware that composes bot detection and rate limiting.

Usage:
    from fastapi import FastAPI
    from src.security import SecurityMiddleware

    app = FastAPI()
    app.add_middleware(
        SecurityMiddleware,
        rate_limiter=RateLimiter(...),
        bot_detector=BotDetector(...),
    )
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.security.detector import BotDetector
from src.security.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

_BLOCKED_UA_MSG = "Access denied — automated/bot traffic detected"
_BLOCKED_VELOCITY_MSG = "Access denied — request velocity exceeded"
_TOO_MANY_REQUESTS_MSG = "Too Many Requests"
_HEADER_RETRY_AFTER = "Retry-After"
_HEADER_X_RATE_LIMIT = "X-RateLimit-Limit"
_HEADER_X_RATE_REMAINING = "X-RateLimit-Remaining"


def _blocked_response(
    status_code: int,
    message: str,
    retry_after: int = 60,
    detail: str | None = None,
) -> JSONResponse:
    """Build a structured JSON error response."""
    body: dict = {"code": status_code, "message": message}
    if retry_after:
        body["retry_after"] = retry_after
    if detail:
        body["detail"] = detail
    response = JSONResponse(status_code=status_code, content=body)
    if retry_after:
        response.headers[_HEADER_RETRY_AFTER] = str(retry_after)
    return response


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class SecurityMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces bot detection and rate limits.

    Processing order:
        1. Extract client IP from ``X-Forwarded-For`` or ``request.client``.
        2. Run User-Agent bot detection — block immediately on match.
        3. Run velocity check — block if threshold exceeded.
        4. Run route-based rate limiting — 429 if over limit.

    Args:
        app: The inner ASGI application.
        rate_limiter: A configured :class:`RateLimiter` instance.
        bot_detector: A configured :class:`BotDetector` instance.
        trusted_proxy: IP / CIDR of a reverse proxy whose
            ``X-Forwarded-For`` header is trusted.  When provided the
            right-most entry is used as the client IP.
        exclude_paths: Set of path prefixes to skip entirely (e.g.
            ``/health``, ``/metrics``).
    """

    def __init__(
        self,
        app,
        *,
        rate_limiter: RateLimiter | None = None,
        bot_detector: BotDetector | None = None,
        trusted_proxy: str | None = None,
        exclude_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._rate_limiter = rate_limiter or RateLimiter()
        self._bot_detector = bot_detector or BotDetector()
        self._trusted_proxy = trusted_proxy
        self._exclude_paths = exclude_paths or {
            "/health", "/status", "/metrics", "/favicon.ico",
        }

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # -- Skip excluded paths ---------------------------------------------------
        if any(request.url.path.startswith(p) for p in self._exclude_paths):
            return await call_next(request)

        client_ip = self._extract_ip(request)
        route_name = request.scope.get("route", None)
        route_name = (
            getattr(route_name, "name", None) or request.url.path.lstrip("/")
        )

        # -- 1. Bot UA check -------------------------------------------------------
        ua = request.headers.get("User-Agent")
        ua_result = self._bot_detector.check_user_agent(ua)
        if ua_result.is_bot:
            logger.info(
                "Bot UA blocked",
                extra={
                    "client_ip": client_ip,
                    "user_agent": (ua or "")[:200],
                    "pattern": ua_result.matched_pattern,
                },
            )
            return _blocked_response(
                status_code=403,
                message=_BLOCKED_UA_MSG,
                retry_after=0,
                detail=ua_result.reason,
            )

        # -- 2. Velocity check -----------------------------------------------------
        vel_result = self._bot_detector.check_velocity(client_ip)
        if vel_result.velocity_block:
            logger.info(
                "Velocity block",
                extra={
                    "client_ip": client_ip,
                    "count": vel_result.velocity_count,
                },
            )
            return _blocked_response(
                status_code=429,
                message=_BLOCKED_VELOCITY_MSG,
                retry_after=self._bot_detector._block_secs,
                detail=vel_result.reason,
            )

        # -- 3. Rate limiting ------------------------------------------------------
        allowed, rl_error = await self._rate_limiter.check(route_name, client_ip)
        if not allowed and rl_error is not None:
            return _blocked_response(
                status_code=rl_error.code,
                message=rl_error.message,
                retry_after=rl_error.retry_after,
                detail=rl_error.detail,
            )

        # -- Process request -------------------------------------------------------
        response: Response = await call_next(request)

        # Inject rate-limit usage headers
        used, limit, window = await self._rate_limiter.get_usage(
            route_name, client_ip
        )
        response.headers[_HEADER_X_RATE_LIMIT] = str(limit)
        response.headers[_HEADER_X_RATE_REMAINING] = str(max(0, limit - used))

        return response

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_ip(request: Request) -> str:
        """Best-effort client IP extraction."""
        # X-Forwarded-For (first entry is typically the origin)
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        # Fall back to the direct client host
        client = request.client
        if client:
            return client.host or "unknown"
        return "unknown"
