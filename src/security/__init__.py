"""Security Agent — FastAPI middleware for rate limiting and intrusion detection.

Provides:
    - SecurityMiddleware: FastAPI ASGI middleware combining bot detection,
      velocity checks, and rate limiting.
    - BotDetector: User-Agent pattern matching and request velocity analysis.
    - RateLimiter: Redis-backed sliding window rate limiter with configurable
      per-route limits.
    - UploadValidator: File upload validation (MIME, size, content scan).

Error responses follow the structured format:
    {"code": 429, "message": "...", "retry_after": 30}
"""

from src.security.detector import BotDetector
from src.security.middleware import SecurityMiddleware
from src.security.ratelimit import RateLimiter
from src.security.validator import UploadValidator

__all__ = [
    "BotDetector",
    "RateLimiter",
    "SecurityMiddleware",
    "UploadValidator",
]
