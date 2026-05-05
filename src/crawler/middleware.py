"""Anti-blocking middleware for Chinese government website scraping.

Implements:
  - User-Agent rotation
  - Request throttling (max 3 req/s)
  - Exponential backoff for 429/403 responses
  - Proxy rotation (every 5 minutes)
  - Graceful handling of rate-limit pages
  - structlog-structured logging
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from typing import Optional

import scrapy
import structlog
from scrapy import signals
from scrapy.crawler import Crawler
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.http import Request, Response
from scrapy.utils.response import response_status_message
from twisted.internet import defer
from twisted.internet.error import (
    ConnectError,
    ConnectionDone,
    ConnectionLost,
    DNSLookupError,
    TCPTimedOutError,
    TimeoutError,
)

logger = structlog.get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# User-Agent Pool
# ═══════════════════════════════════════════════════════════════════════════════

_USER_AGENTS: list[str] = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Chrome on Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.45 Mobile Safari/537.36",
]

_EXCLUDED_HEADERS: set[bytes] = {
    b"accept-encoding",
    b"sec-ch-ua",
    b"sec-ch-ua-mobile",
    b"sec-ch-ua-platform",
}


def _random_ua() -> str:
    """Return a random User-Agent from the pool."""
    return random.choice(_USER_AGENTS)


# ═══════════════════════════════════════════════════════════════════════════════
# Proxy Rotation
# ═══════════════════════════════════════════════════════════════════════════════


class ProxyPool:
    """Manages a pool of HTTP proxies with rotation and health tracking."""

    def __init__(self, proxy_urls: list[str], rotation_interval: int = 300) -> None:
        """Initialize the proxy pool.

        Args:
            proxy_urls: List of proxy URLs (e.g. 'http://user:pass@host:port').
            rotation_interval: Seconds between rotations.
        """
        self._proxies: list[str] = list(proxy_urls)
        self._failures: dict[str, int] = {}
        self._blacklist: set[str] = set()
        self._rotation_interval = rotation_interval
        self._last_rotation = time.monotonic()
        self._current_index = 0

    @property
    def has_proxies(self) -> bool:
        return len(self._proxies) > 0

    def get_proxy(self) -> Optional[str]:
        """Return the current proxy, rotating if interval elapsed."""
        if not self._proxies:
            return None

        now = time.monotonic()
        if now - self._last_rotation >= self._rotation_interval:
            self._current_index = (self._current_index + 1) % len(self._proxies)
            self._last_rotation = now
            logger.debug("proxy_rotated", index=self._current_index)

        return self._proxies[self._current_index]

    def mark_failure(self, proxy_url: str) -> None:
        """Record a failure for a proxy; blacklist after 3 consecutive failures."""
        self._failures[proxy_url] = self._failures.get(proxy_url, 0) + 1
        if self._failures[proxy_url] >= 3:
            self._blacklist.add(proxy_url)
            if proxy_url in self._proxies:
                self._proxies.remove(proxy_url)
            logger.warning("proxy_blacklisted", proxy=proxy_url)

    def mark_success(self, proxy_url: str) -> None:
        """Reset failure count for a proxy on success."""
        self._failures[proxy_url] = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Throttle Token Bucket
# ═══════════════════════════════════════════════════════════════════════════════


class TokenBucket:
    """Token bucket algorithm for per-domain rate limiting."""

    def __init__(self, rate: float = 3.0, burst: int = 5) -> None:
        """Initialize token bucket.

        Args:
            rate: Tokens (requests) per second.
            burst: Maximum burst size.
        """
        self._rate = rate
        self._burst = burst
        self._tokens: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, domain: str) -> None:
        """Wait until a token is available for the given domain.

        If the bucket is empty, sleeps until a token refills.
        """
        async with self._lock:
            now = time.monotonic()
            tokens = self._tokens.get(domain, self._burst)

            if tokens < 1.0:
                wait = (1.0 - tokens) / self._rate
                self._tokens[domain] = 0.0
            else:
                self._tokens[domain] = tokens - 1.0
                wait = 0.0

            # Refill tokens based on elapsed time
            if domain not in self._tokens:
                self._tokens[domain] = float(self._burst)
            else:
                elapsed = now - getattr(self, "_last_refill", now)
                refill = elapsed * self._rate
                self._tokens[domain] = min(
                    float(self._burst), self._tokens[domain] + refill
                )
            self._last_refill = now  # type: ignore[attr-defined]

        if wait > 0:
            await asyncio.sleep(wait)


# ═══════════════════════════════════════════════════════════════════════════════
# Scrapy Middlewares
# ═══════════════════════════════════════════════════════════════════════════════


class UserAgentRotationMiddleware:
    """Rotates User-Agent header on every request."""

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "UserAgentRotationMiddleware":
        return cls()

    def process_request(self, request: Request, spider: scrapy.Spider) -> None:
        """Set a random User-Agent header before each request."""
        request.headers[b"User-Agent"] = _random_ua().encode("utf-8")
        # Scrub browser-fingerprinting headers that might reveal automation
        for header in _EXCLUDED_HEADERS:
            request.headers.pop(header, None)


class ThrottleMiddleware:
    """Token-bucket rate limiter enforcing ~3 requests/second per domain."""

    def __init__(self) -> None:
        self._bucket = TokenBucket(rate=3.0, burst=5)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "ThrottleMiddleware":
        return cls()

    def process_request(self, request: Request, spider: scrapy.Spider) -> Optional[defer.Deferred]:
        """Delay the request if the domain's token bucket is empty.

        Returns a Deferred that fires after the required delay.
        """
        from urllib.parse import urlparse

        domain = urlparse(request.url).netloc

        async def _wait_and_proceed() -> None:
            await self._bucket.acquire(domain)

        # Scrapy runs on Twisted; convert asyncio coroutine to Deferred
        d = defer.Deferred()
        task = asyncio.ensure_future(_wait_and_proceed())

        def _on_done(_fut: asyncio.Future[None]) -> None:
            try:
                _fut.result()
            except Exception as exc:
                logger.error("throttle_error", error=str(exc))
            d.callback(None)

        task.add_done_callback(_on_done)
        return d


class RateLimitRetryMiddleware(RetryMiddleware):
    """Custom retry middleware with exponential backoff for 429/403.

    Extends Scrapy's built-in RetryMiddleware with:
      - Longer, jittered backoff delays
      - Proxy failure tracking
      - Respect for Retry-After headers
    """

    def __init__(self, settings: scrapy.settings.Settings) -> None:
        super().__init__(settings)
        self._max_retry_times = settings.getint("RETRY_TIMES", 5)
        self._retry_http_codes = set(
            settings.getlist("RETRY_HTTP_CODES", [429, 500, 502, 503, 504, 408, 403])
        )
        # Track retry counts per URL for exponential backoff
        self._retry_counts: dict[str, int] = {}

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "RateLimitRetryMiddleware":
        return cls(crawler.settings)

    def _backoff_delay(self, retry_count: int) -> float:
        """Compute exponential backoff with jitter.

        Formula: base_delay * 2^(retry_count-1) + random jitter [0, 1)s
        Clamped to [1, 300] seconds.
        """
        base = 2.0
        delay = base * (2 ** (retry_count - 1))
        jitter = random.uniform(0, 1.0)
        return min(delay + jitter, 300.0)

    def _parse_retry_after(self, response: Response) -> Optional[float]:
        """Extract Retry-After header value, if present."""
        retry_after = response.headers.get(b"Retry-After")
        if retry_after is None:
            return None
        try:
            return float(retry_after)
        except (ValueError, TypeError):
            return None

    def process_response(
        self, request: Request, response: Response, spider: scrapy.Spider
    ) -> Response | Request:
        """Check response status and retry with backoff if rate-limited."""
        if response.status not in self._retry_http_codes:
            return response

        url_hash = hashlib.sha256(request.url.encode()).hexdigest()[:16]
        retry_count = self._retry_counts.get(url_hash, 0) + 1

        if retry_count > self._max_retry_times:
            logger.error(
                "max_retries_exceeded",
                url=request.url,
                status=response.status,
                retries=retry_count - 1,
            )
            self._retry_counts.pop(url_hash, None)
            return response  # Give up; let pipeline handle

        self._retry_counts[url_hash] = retry_count

        # Use Retry-After if provided, else exponential backoff
        retry_after = self._parse_retry_after(response)
        delay = retry_after if retry_after is not None else self._backoff_delay(retry_count)

        logger.warning(
            "retrying_request",
            url=request.url,
            status=response.status,
            attempt=retry_count,
            max_retries=self._max_retry_times,
            backoff_seconds=round(delay, 2),
            reason=response_status_message(response.status),
        )

        # Check for anti-bot signals in the response body
        body_sample = response.text[:1000].lower()
        for signal_text in ("访问频率", "too many requests", "rate limit", "验证码", "captcha"):
            if signal_text in body_sample:
                logger.warning(
                    "anti_bot_detected",
                    url=request.url,
                    signal=signal_text,
                )

        time.sleep(delay)
        retry_req = request.copy()
        retry_req.dont_filter = True
        return retry_req

    def process_exception(
        self, request: Request, exception: Exception, spider: scrapy.Spider
    ) -> Optional[Response | Request | defer.Deferred]:
        """Handle network-level exceptions with retries."""
        url_hash = hashlib.sha256(request.url.encode()).hexdigest()[:16]
        retry_count = self._retry_counts.get(url_hash, 0) + 1

        if retry_count > self._max_retry_times:
            logger.error(
                "max_retries_exceeded_network",
                url=request.url,
                exception_type=type(exception).__name__,
                error=str(exception),
                retries=retry_count - 1,
            )
            self._retry_counts.pop(url_hash, None)
            return None

        self._retry_counts[url_hash] = retry_count
        delay = self._backoff_delay(retry_count)

        logger.warning(
            "retrying_network_error",
            url=request.url,
            exception_type=type(exception).__name__,
            error=str(exception),
            attempt=retry_count,
            backoff_seconds=round(delay, 2),
        )

        time.sleep(delay)
        retry_req = request.copy()
        retry_req.dont_filter = True
        return retry_req


# ═══════════════════════════════════════════════════════════════════════════════
# Structlog Integration
# ═══════════════════════════════════════════════════════════════════════════════


class StructlogLogging:
    """Scrapy extension that configures structlog for all spider events."""

    def __init__(self) -> None:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.dev.ConsoleRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "StructlogLogging":
        ext = cls()
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_opened(self, spider: scrapy.Spider) -> None:
        logger.info("spider_opened", spider=spider.name)

    def spider_closed(self, spider: scrapy.Spider, reason: str) -> None:
        logger.info("spider_closed", spider=spider.name, reason=reason)
