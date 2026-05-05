"""Bot and intrusion detection — User-Agent analysis and velocity checks.

Detects automated/bot traffic by:
    1. Matching User-Agent strings against known bot patterns.
    2. Monitoring per-IP request velocity within a sliding time window.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import ClassVar, Pattern

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled patterns for bot / automation User-Agents
# ---------------------------------------------------------------------------

_BOT_PATTERNS: list[Pattern[str]] = [
    # Use (?:^|[^a-zA-Z]) instead of \b so HeadlessChrome and Googlebot match
    re.compile(r"(?:^|[^a-zA-Z])python-requests(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])scrapy(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])curl(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])headless(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])selenium(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])wget(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])httpx(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])aiohttp(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])go-http-client(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])axios(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])okhttp(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])request\.py(?:$|[^a-zA-Z])", re.IGNORECASE),
    # Generic bot / spider / crawler keywords
    re.compile(r"(?:^|[^a-zA-Z])bot(?:$|[^a-zA-Z/])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])spider(?:$|[^a-zA-Z])", re.IGNORECASE),
    re.compile(r"(?:^|[^a-zA-Z])crawler(?:$|[^a-zA-Z])", re.IGNORECASE),
]


@dataclass
class DetectionResult:
    """Outcome of a bot-detection analysis."""

    is_bot: bool
    matched_pattern: str | None = None
    velocity_count: int = 0
    velocity_block: bool = False
    reason: str = ""


@dataclass
class VelocityWindow:
    """Sliding tracking window per IP address."""

    hits: list[float] = field(default_factory=list)
    blocked_until: float = 0.0

    def record(self, now: float, window_secs: float) -> None:
        """Append a hit and prune stale entries outside the window."""
        self.hits.append(now)
        self._prune(now, window_secs)

    def count(self, now: float, window_secs: float) -> int:
        """Return the number of hits within the window after pruning."""
        self._prune(now, window_secs)
        return len(self.hits)

    def _prune(self, now: float, window_secs: float) -> None:
        cutoff = now - window_secs
        while self.hits and self.hits[0] < cutoff:
            self.hits.pop(0)


class BotDetector:
    """Analyzes requests for bot-like behaviour.

    Combines static User-Agent pattern matching with dynamic per-IP
    velocity tracking.

    Args:
        ua_patterns: Custom regex patterns for User-Agent matching.
        velocity_window_secs: Sliding window duration for the velocity check
            (default: 10 seconds).
        velocity_threshold: Number of requests allowed within the window
            before the IP is flagged (default: 10).
        block_duration_secs: How long a flagged IP stays blocked
            (default: 60 seconds).
    """

    DEFAULT_WINDOW_SECS: ClassVar[int] = 10
    DEFAULT_VELOCITY_THRESHOLD: ClassVar[int] = 10
    DEFAULT_BLOCK_SECS: ClassVar[int] = 60

    def __init__(
        self,
        ua_patterns: list[Pattern[str]] | None = None,
        velocity_window_secs: int = DEFAULT_WINDOW_SECS,
        velocity_threshold: int = DEFAULT_VELOCITY_THRESHOLD,
        block_duration_secs: int = DEFAULT_BLOCK_SECS,
    ) -> None:
        self._patterns: list[Pattern[str]] = ua_patterns or _BOT_PATTERNS
        self._window_secs = velocity_window_secs
        self._threshold = velocity_threshold
        self._block_secs = block_duration_secs
        # In production this would use Redis; in-memory is used as a fallback
        self._windows: dict[str, VelocityWindow] = defaultdict(VelocityWindow)

    # -- User-Agent analysis -------------------------------------------------

    def check_user_agent(self, ua: str | None) -> DetectionResult:
        """Test a User-Agent string against known bot patterns.

        Args:
            ua: The ``User-Agent`` header value (may be ``None``).

        Returns:
            :class:`DetectionResult` with ``is_bot=True`` if a pattern matched.
        """
        if not ua:
            return DetectionResult(is_bot=False)
        for pat in self._patterns:
            if pat.search(ua):
                return DetectionResult(
                    is_bot=True,
                    matched_pattern=pat.pattern,
                    reason=f"UA matches bot pattern: {pat.pattern}",
                )
        return DetectionResult(is_bot=False)

    # -- Velocity checking ---------------------------------------------------

    def check_velocity(self, client_ip: str) -> DetectionResult:
        """Record a hit for *client_ip* and test the velocity threshold.

        Returns a blocking result when the IP exceeds the threshold within
        the configured sliding window.

        Args:
            client_ip: The caller's IP address.

        Returns:
            :class:`DetectionResult` with ``velocity_block=True`` on
            violation.
        """
        now = time.monotonic()
        window = self._windows[client_ip]

        # If currently under an active block, extend it
        if window.blocked_until > now:
            return DetectionResult(
                is_bot=True,
                velocity_block=True,
                velocity_count=window.count(now, self._window_secs),
                reason=f"IP {client_ip} is temporarily blocked "
                f"({self._block_secs}s)",
            )

        window.record(now, self._window_secs)
        count = window.count(now, self._window_secs)

        if count > self._threshold:
            window.blocked_until = now + self._block_secs
            logger.warning(
                "Velocity threshold exceeded",
                extra={
                    "client_ip": client_ip,
                    "count": count,
                    "threshold": self._threshold,
                    "window_secs": self._window_secs,
                },
            )
            return DetectionResult(
                is_bot=True,
                velocity_block=True,
                velocity_count=count,
                reason=(
                    f"Velocity exceeded: {count} req in "
                    f"{self._window_secs}s (limit: {self._threshold})"
                ),
            )

        return DetectionResult(is_bot=False, velocity_count=count)

    def get_velocity_count(self, client_ip: str) -> int:
        """Return the current in-window hit count for *client_ip*."""
        now = time.monotonic()
        return self._windows[client_ip].count(now, self._window_secs)

    def reset_ip(self, client_ip: str) -> None:
        """Clear velocity tracking for an IP (e.g. after a successful auth)."""
        self._windows.pop(client_ip, None)
