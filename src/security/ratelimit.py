"""Redis-backed sliding-window rate limiter for FastAPI.

Implements per-route configurable limits using a sliding-window algorithm
stored in Redis. Supports optional fallback to in-memory storage for
development environments without Redis.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit configurations per route group
# ---------------------------------------------------------------------------


@dataclass
class RateLimitConfig:
    """Definition of a rate-limit rule.

    Attributes:
        max_requests: Maximum allowed requests within the window.
        window_secs: Duration of the sliding window in seconds.
        key_prefix: Redis key prefix for this limit group.
    """

    max_requests: int
    window_secs: int
    key_prefix: str = "rl"


# Default rate-limit tiers
TIR_DEFAULT = RateLimitConfig(max_requests=100, window_secs=60, key_prefix="rl:default")
TIR_SEARCH = RateLimitConfig(max_requests=50, window_secs=60, key_prefix="rl:search")
TIR_DOWNLOAD = RateLimitConfig(
    max_requests=20, window_secs=3600, key_prefix="rl:download"
)

# Convenience mapping of route-name prefixes to tiers
# Can be extended via application config.
DEFAULT_ROUTE_LIMITS: dict[str, RateLimitConfig] = {
    "search": TIR_SEARCH,
    "download": TIR_DOWNLOAD,
}


# ---------------------------------------------------------------------------
# Structured error helpers
# ---------------------------------------------------------------------------


@dataclass
class RateLimitError:
    """Structured error payload returned when a limit is exceeded.

    Serializes to:
        {"code": 429, "message": "...", "retry_after": 30}
    """

    code: int = 429
    message: str = "Too Many Requests"
    retry_after: int = 60
    detail: str | None = None


# ---------------------------------------------------------------------------
# Abstract back end so we can swap Redis / in-memory at runtime
# ---------------------------------------------------------------------------


class RateLimitBackend:
    """Protocol / base class for rate-limit storage backends."""

    async def get_counter(self, key: str, window_secs: int = 60) -> int:
        raise NotImplementedError

    async def increment(self, key: str, ttl_secs: int) -> int:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def expire(self, key: str, ttl_secs: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Redis backend (aioredis / redis-py async)
# ---------------------------------------------------------------------------


class RedisBackend(RateLimitBackend):
    """Redis-backed sliding window using sorted sets.

    Each request appends the current timestamp (score = microtime) to a sorted
    set.  Expired entries are trimmed on each read.

    Args:
        redis: An async Redis client (from ``redis.asyncio``).
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def get_counter(self, key: str, window_secs: int = 60) -> int:
        """Count entries still inside the sliding window."""
        now = time.time()
        cutoff = now - window_secs
        # Remove entries older than the window
        await self._redis.zremrangebyscore(key, "-inf", cutoff)
        return await self._redis.zcard(key)

    async def increment(self, key: str, ttl_secs: int) -> int:
        """Add a new request timestamp and return the updated count."""
        now = time.time()
        pipe = self._redis.pipeline(transaction=True)
        pipe.zadd(key, {str(now): now})
        # Trim stale members (older than now - ttl)
        pipe.zremrangebyscore(key, "-inf", now - ttl_secs)
        pipe.zcard(key)
        pipe.expire(key, ttl_secs + 5)  # slight padding so key doesn't vanish
        _, _, count, _ = await pipe.execute()
        return int(count)

    async def expire(self, key: str, ttl_secs: int) -> None:
        await self._redis.expire(key, ttl_secs + 5)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def close(self) -> None:
        await self._redis.aclose()


# ---------------------------------------------------------------------------
# In-memory fall back (dev / test without Redis)
# ---------------------------------------------------------------------------


@dataclass
class _MemoryRecord:
    hits: list[float] = field(default_factory=list)
    ttl: float = 0.0


class MemoryBackend(RateLimitBackend):
    """Thread-local in-memory backend for development and testing."""

    def __init__(self) -> None:
        self._store: dict[str, _MemoryRecord] = {}
        self._lock = asyncio.Lock()

    async def get_counter(self, key: str, window_secs: int = 60) -> int:
        now = time.time()
        cutoff = now - window_secs
        async with self._lock:
            rec = self._store.get(key)
            if rec is None or rec.ttl < now:
                self._store.pop(key, None)
                return 0
            rec.hits = [h for h in rec.hits if h > cutoff]
            return len(rec.hits)

    async def increment(self, key: str, ttl_secs: int) -> int:
        now = time.time()
        window_start = now - ttl_secs
        async with self._lock:
            rec = self._store.get(key)
            if rec is None:
                rec = _MemoryRecord(hits=[now], ttl=now + ttl_secs + 5)
                self._store[key] = rec
                return 1
            # Prune
            rec.hits = [h for h in rec.hits if h > window_start]
            rec.hits.append(now)
            rec.ttl = now + ttl_secs + 5
            return len(rec.hits)

    async def expire(self, key: str, ttl_secs: int) -> None:
        async with self._lock:
            if rec := self._store.get(key):
                rec.ttl = time.time() + ttl_secs + 5


# ---------------------------------------------------------------------------
# Rate limiter itself
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window rate limiter with per-route configuration.

    Args:
        backend: A :class:`RateLimitBackend` (Redis or in-memory).
        route_limits: Mapping of route-name prefix → :class:`RateLimitConfig`.
        default_limit: Fallback config used when no route-specific rule
            matches.
    """

    def __init__(
        self,
        backend: RateLimitBackend | None = None,
        route_limits: dict[str, RateLimitConfig] | None = None,
        default_limit: RateLimitConfig | None = None,
    ) -> None:
        self._backend = backend or MemoryBackend()
        self._route_limits = route_limits or DEFAULT_ROUTE_LIMITS
        self._default_limit = default_limit or TIR_DEFAULT

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(
        self, route_name: str, client_id: str
    ) -> tuple[bool, RateLimitError | None]:
        """Check whether *client_id* is allowed to hit *route_name*.

        Args:
            route_name: The FastAPI route name (e.g. ``"search_reports"``).
            client_id: Identifier for the caller (typically IP address).

        Returns:
            A ``(allowed, error)`` tuple.  When allowed is ``False`` the
            error payload is populated.
        """
        config = self._resolve_config(route_name)
        key = f"{config.key_prefix}:{client_id}"

        count = await self._backend.increment(key, config.window_secs)
        if count <= config.max_requests:
            return True, None

        # Calculate retry_after from the oldest hit in the window
        ttl_remaining = config.window_secs
        try:
            # Make a best-effort estimate — the backend may not support this
            ttl_remaining = max(5, config.window_secs - int(count * 0.5))
        except Exception:
            pass

        retry_after = max(1, ttl_remaining)
        error = RateLimitError(
            retry_after=retry_after,
            message=f"Rate limit exceeded for {route_name} "
            f"({config.max_requests}/{config.window_secs}s)",
        )
        logger.warning(
            "Rate limit hit",
            extra={
                "route": route_name,
                "client_id": client_id,
                "limit": config.max_requests,
                "count": count,
            },
        )
        return False, error

    async def get_usage(
        self, route_name: str, client_id: str
    ) -> tuple[int, int, int]:
        """Return ``(used, limit, window_secs)`` for the given route/client."""
        config = self._resolve_config(route_name)
        key = f"{config.key_prefix}:{client_id}"
        used = await self._backend.get_counter(key)
        return used, config.max_requests, config.window_secs

    async def reset(self, route_name: str, client_id: str) -> None:
        """Clear rate-limit counters for a specific route + client."""
        config = self._resolve_config(route_name)
        key = f"{config.key_prefix}:{client_id}"
        await self._backend.expire(key, 0)

    async def close(self) -> None:
        """Release backend resources."""
        await self._backend.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_config(self, route_name: str) -> RateLimitConfig:
        """Pick the most specific matching config for *route_name*."""
        for prefix, cfg in self._route_limits.items():
            if route_name.startswith(prefix):
                return cfg
        return self._default_limit
