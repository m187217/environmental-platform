"""Tests for the Security Agent — all components.

Run with:
    pytest src/security/tests/test_security.py -v
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from src.security.detector import BotDetector, DetectionResult, VelocityWindow
from src.security.middleware import SecurityMiddleware, _blocked_response
from src.security.ratelimit import (
    MemoryBackend,
    RateLimiter,
    RateLimitConfig,
    RateLimitError,
    TIR_DEFAULT,
    TIR_DOWNLOAD,
    TIR_SEARCH,
)
from src.security.validator import (
    ALLOWED_MIME_TYPES,
    UploadValidator,
    ValidationResult,
    ValidationStatus,
)

# ============================================================================
# BotDetector
# ============================================================================


class TestBotDetector:
    """Tests for :class:`BotDetector`."""

    def test_ua_python_requests_detected(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent("python-requests/2.31.0")
        assert result.is_bot is True
        assert "python-requests" in (result.matched_pattern or "")

    def test_ua_scrapy_detected(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent("Scrapy/2.11.0 (+https://scrapy.org)")
        assert result.is_bot is True

    def test_ua_curl_detected(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent("curl/8.4.0")
        assert result.is_bot is True

    def test_ua_headless_chrome_detected(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent(
            "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120.0"
        )
        assert result.is_bot is True

    def test_ua_selenium_detected(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent("selenium/4.15.0 Python/3.11")
        assert result.is_bot is True

    def test_ua_legitimate_browser_allowed(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        assert result.is_bot is False

    def test_ua_none_allowed(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent(None)
        assert result.is_bot is False

    def test_ua_empty_allowed(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent("")
        assert result.is_bot is False

    def test_ua_bot_keyword_detected(self) -> None:
        detector = BotDetector()
        result = detector.check_user_agent("Googlebot/2.1")
        assert result.is_bot is True

    def test_custom_patterns(self) -> None:
        import re

        custom = [re.compile(r"my-custom-scraper", re.IGNORECASE)]
        detector = BotDetector(ua_patterns=custom)
        result = detector.check_user_agent("my-custom-scraper/1.0")
        assert result.is_bot is True

    # -- Velocity checks ----------------------------------------------------

    def test_velocity_under_threshold(self) -> None:
        detector = BotDetector(
            velocity_window_secs=10, velocity_threshold=10, block_duration_secs=5
        )
        for _ in range(5):
            result = detector.check_velocity("192.168.1.1")
            assert not result.velocity_block

    def test_velocity_exceeds_threshold(self) -> None:
        detector = BotDetector(
            velocity_window_secs=10, velocity_threshold=5, block_duration_secs=60
        )
        for i in range(6):
            result = detector.check_velocity("10.0.0.1")
            if i < 5:
                assert not result.velocity_block, f"Hit {i} should pass"
        # The 6th call (index 5) exceeds threshold of 5
        assert result.velocity_block is True

    def test_velocity_block_persists(self) -> None:
        detector = BotDetector(
            velocity_window_secs=10, velocity_threshold=2, block_duration_secs=60
        )
        detector.check_velocity("1.2.3.4")
        detector.check_velocity("1.2.3.4")
        result = detector.check_velocity("1.2.3.4")  # exceeds 2
        assert result.velocity_block is True
        # Subsequent calls within block period should also be blocked
        result2 = detector.check_velocity("1.2.3.4")
        assert result2.velocity_block is True

    def test_velocity_reset_ip(self) -> None:
        detector = BotDetector(
            velocity_window_secs=10, velocity_threshold=3, block_duration_secs=5
        )
        for _ in range(4):
            result = detector.check_velocity("10.10.10.10")
        assert result.velocity_block is True
        detector.reset_ip("10.10.10.10")
        result2 = detector.check_velocity("10.10.10.10")
        assert not result2.velocity_block

    def test_velocity_independent_ips(self) -> None:
        detector = BotDetector(
            velocity_window_secs=10, velocity_threshold=3, block_duration_secs=60
        )
        for _ in range(4):
            detector.check_velocity("192.168.1.100")
        # Different IP should not be affected
        result = detector.check_velocity("192.168.1.200")
        assert not result.velocity_block


class TestVelocityWindow:
    """Low-level tests for :class:`VelocityWindow`."""

    def test_basic_counting(self) -> None:
        win = VelocityWindow()
        now = 100.0
        for _ in range(5):
            win.record(now, 10.0)
        assert win.count(now, 10.0) == 5

    def test_pruning_expired(self) -> None:
        win = VelocityWindow()
        now = 100.0
        # Record hits at t=90 (just inside 10s window from t=100)
        win.hits.append(90.0)
        win.hits.append(92.0)
        assert win.count(now, 10.0) == 2
        # At t=105, both 90 and 92 are outside the 10s window (cutoff=95)
        assert win.count(105.0, 10.0) == 0


# ============================================================================
# RateLimiter
# ============================================================================


class TestRateLimiter:
    """Tests for :class:`RateLimiter`."""

    @pytest.mark.asyncio
    async def test_allowed_within_limit(self) -> None:
        limiter = RateLimiter(backend=MemoryBackend())
        for _ in range(10):
            allowed, error = await limiter.check("test_route", "127.0.0.1")
            assert allowed is True
            assert error is None

    @pytest.mark.asyncio
    async def test_blocked_exceeding_limit(self) -> None:
        limiter = RateLimiter(
            backend=MemoryBackend(),
            default_limit=RateLimitConfig(max_requests=3, window_secs=60),
        )
        for _ in range(3):
            allowed, _ = await limiter.check("test_route", "10.0.0.1")
            assert allowed is True
        allowed, error = await limiter.check("test_route", "10.0.0.1")
        assert allowed is False
        assert error is not None
        assert error.code == 429
        assert error.retry_after > 0

    @pytest.mark.asyncio
    async def test_search_tier(self) -> None:
        limiter = RateLimiter(backend=MemoryBackend())
        # Search routes get TIR_SEARCH (50/min)
        for _ in range(50):
            allowed, _ = await limiter.check("search_reports", "192.168.1.1")
            assert allowed is True
        allowed, error = await limiter.check("search_reports", "192.168.1.1")
        assert allowed is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_download_tier(self) -> None:
        limiter = RateLimiter(backend=MemoryBackend())
        # Download routes get TIR_DOWNLOAD (20/hour)
        for _ in range(20):
            allowed, _ = await limiter.check("download_report", "10.0.0.2")
            assert allowed is True
        allowed, error = await limiter.check("download_report", "10.0.0.2")
        assert allowed is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_get_usage(self) -> None:
        limiter = RateLimiter(backend=MemoryBackend())
        for _ in range(7):
            await limiter.check("some_route", "1.1.1.1")
        used, limit, window = await limiter.get_usage("some_route", "1.1.1.1")
        assert used == 7
        assert limit == 100  # default
        assert window == 60

    @pytest.mark.asyncio
    async def test_reset(self) -> None:
        limiter = RateLimiter(
            backend=MemoryBackend(),
            default_limit=RateLimitConfig(max_requests=5, window_secs=60),
        )
        for _ in range(5):
            await limiter.check("route_x", "10.0.0.99")
        allowed, _ = await limiter.check("route_x", "10.0.0.99")
        assert allowed is False
        await limiter.reset("route_x", "10.0.0.99")
        allowed, _ = await limiter.check("route_x", "10.0.0.99")
        assert allowed is True

    @pytest.mark.asyncio
    async def test_independent_clients(self) -> None:
        limiter = RateLimiter(
            backend=MemoryBackend(),
            default_limit=RateLimitConfig(max_requests=3, window_secs=60),
        )
        for _ in range(4):
            await limiter.check("route_a", "192.168.1.1")
        # Client 2 is independent and should still be allowed
        allowed, _ = await limiter.check("route_a", "192.168.1.2")
        assert allowed is True

    def test_rate_limit_error_model(self) -> None:
        error = RateLimitError(
            message="Blocked", retry_after=30, detail="Extra info"
        )
        assert error.code == 429
        assert error.message == "Blocked"
        assert error.retry_after == 30
        assert error.detail == "Extra info"


# ============================================================================
# SecurityMiddleware (integration)
# ============================================================================


class TestSecurityMiddleware:
    """Integration tests for the composed middleware."""

    @pytest.fixture
    def app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/data")
        async def data():
            return {"data": [1, 2, 3]}

        @app.get("/search")
        async def search():
            return {"results": []}

        @app.get("/download/report")
        async def download():
            return {"file": "report.pdf"}

        return app

    @pytest.fixture
    def secure_app(self, app: FastAPI) -> FastAPI:
        bot_detector = BotDetector()
        rate_limiter = RateLimiter(backend=MemoryBackend())
        app.add_middleware(
            SecurityMiddleware,
            rate_limiter=rate_limiter,
            bot_detector=bot_detector,
        )
        return app

    def test_health_excluded(self, secure_app: FastAPI) -> None:
        client = TestClient(secure_app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_bot_ua_blocked(self, secure_app: FastAPI) -> None:
        client = TestClient(secure_app)
        response = client.get(
            "/data", headers={"User-Agent": "curl/8.4.0"}
        )
        assert response.status_code == 403
        body = response.json()
        assert body["code"] == 403
        assert "bot" in body["message"].lower()

    def test_legitimate_request_allowed(self, secure_app: FastAPI) -> None:
        client = TestClient(secure_app)
        response = client.get(
            "/data",
            headers={
                "User-Agent": "Mozilla/5.0 Chrome/120",
                "X-Forwarded-For": "203.0.113.42",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"data": [1, 2, 3]}

    def test_rate_limit_headers_present(self, secure_app: FastAPI) -> None:
        client = TestClient(secure_app)
        response = client.get("/data")
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers

    def test_blocked_response_helper(self) -> None:
        resp = _blocked_response(429, "Too many", retry_after=45)
        assert resp.status_code == 429
        body = resp.body.decode()
        assert "429" in body
        assert "Too many" in body
        assert "45" in body
        assert resp.headers.get("Retry-After") == "45"


# ============================================================================
# UploadValidator
# ============================================================================


class TestUploadValidator:
    """Tests for :class:`UploadValidator`."""

    @pytest.mark.asyncio
    async def test_valid_pdf(self) -> None:
        validator = UploadValidator(max_size_mb=50)
        content = b"%PDF-1.4\nfake pdf content"
        result = await validator.validate("report.pdf", content, "application/pdf")
        assert result.ok is True
        assert result.status == ValidationStatus.OK

    @pytest.mark.asyncio
    async def test_empty_file(self) -> None:
        validator = UploadValidator()
        result = await validator.validate("empty.txt", b"")
        assert not result.ok
        assert result.status == ValidationStatus.EMPTY_FILE

    @pytest.mark.asyncio
    async def test_size_exceeded(self) -> None:
        validator = UploadValidator(max_size_mb=1)  # 1 MB limit
        oversized = b"x" * (2 * 1024 * 1024)  # 2 MB
        result = await validator.validate(
            "large.txt", oversized, "text/plain"
        )
        assert not result.ok
        assert result.status == ValidationStatus.SIZE_EXCEEDED

    @pytest.mark.asyncio
    async def test_mime_not_allowed(self) -> None:
        validator = UploadValidator()
        result = await validator.validate(
            "script.sh", b"#!/bin/bash\necho hi", "application/x-sh"
        )
        assert not result.ok
        assert result.status == ValidationStatus.MIME_NOT_ALLOWED

    @pytest.mark.asyncio
    async def test_extension_mismatch(self) -> None:
        validator = UploadValidator(check_extension=True)
        # Claiming PDF but extension is .txt
        result = await validator.validate(
            "notes.txt", b"%PDF-1.4\n...", "application/pdf"
        )
        assert not result.ok
        assert result.status == ValidationStatus.EXTENSION_MISMATCH

    @pytest.mark.asyncio
    async def test_extension_check_disabled(self) -> None:
        validator = UploadValidator(check_extension=False)
        result = await validator.validate(
            "notes.txt", b"%PDF-1.4\n...", "application/pdf"
        )
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_malicious_php_tag(self) -> None:
        validator = UploadValidator()
        content = b"Some text\n<?php echo 'hello'; ?>\nmore text"
        result = await validator.validate("page.html", content, "text/html")
        assert not result.ok
        assert result.status == ValidationStatus.MALICIOUS_CONTENT
        assert "PHP" in result.error_message

    @pytest.mark.asyncio
    async def test_malicious_script_tag(self) -> None:
        validator = UploadValidator()
        content = b"<html><body><script>alert(1)</script></body></html>"
        result = await validator.validate("x.html", content, "text/html")
        assert not result.ok
        assert result.status == ValidationStatus.MALICIOUS_CONTENT

    @pytest.mark.asyncio
    async def test_binary_no_malicious_scan(self) -> None:
        """Binary files skip the text-based malicious pattern scan."""
        validator = UploadValidator()
        # PNG with a string that would match a malicious pattern in text
        content = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        )
        result = await validator.validate("img.png", content, "image/png")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_sql_injection_pattern(self) -> None:
        validator = UploadValidator()
        content = b"SELECT * FROM users UNION SELECT password FROM admins"
        result = await validator.validate("query.txt", content, "text/plain")
        assert not result.ok
        assert result.status == ValidationStatus.MALICIOUS_CONTENT

    def test_allowed_mime_types_covers_common_formats(self) -> None:
        """Ensure the default whitelist includes key formats."""
        assert "application/pdf" in ALLOWED_MIME_TYPES
        assert "text/csv" in ALLOWED_MIME_TYPES
        assert "image/jpeg" in ALLOWED_MIME_TYPES
        assert "application/zip" in ALLOWED_MIME_TYPES
        assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in ALLOWED_MIME_TYPES

    @pytest.mark.asyncio
    async def test_magic_bytes_detection_pdf(self) -> None:
        validator = UploadValidator()
        result = await validator.validate("file.bin", b"%PDF-1.4 fake")
        assert result.ok is True
        assert result.detected_mime == "application/pdf"

    @pytest.mark.asyncio
    async def test_magic_bytes_detection_png(self) -> None:
        validator = UploadValidator()
        content = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        )
        result = await validator.validate("img.bin", content)
        assert result.ok is True
        assert result.detected_mime == "image/png"


# ============================================================================
# Edge cases & errors
# ============================================================================


class TestEdgeCases:
    """Tests for unusual or boundary conditions."""

    def test_detection_result_defaults(self) -> None:
        result = DetectionResult(is_bot=False)
        assert result.matched_pattern is None
        assert result.velocity_count == 0
        assert result.velocity_block is False

    def test_velocity_window_edge(self) -> None:
        win = VelocityWindow()
        now = 100.0
        # Exactly at the boundary: t=90 is inside the 10s window from t=100
        win.hits.append(90.0)
        assert win.count(100.0, 10.0) == 1
        # t=89.999 is outside
        win2 = VelocityWindow(hits=[89.999])
        assert win2.count(100.0, 10.0) == 0

    @pytest.mark.asyncio
    async def test_rate_limiter_zero_limit(self) -> None:
        limiter = RateLimiter(
            backend=MemoryBackend(),
            default_limit=RateLimitConfig(max_requests=0, window_secs=60),
        )
        allowed, error = await limiter.check("blocked_route", "1.1.1.1")
        assert allowed is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_rate_limiter_non_matching_route_uses_default(self) -> None:
        limiter = RateLimiter(backend=MemoryBackend())
        # "xyz_route" doesn't match any prefix → uses TIR_DEFAULT (100/min)
        for _ in range(100):
            allowed, _ = await limiter.check("xyz_route", "10.10.10.10")
            assert allowed is True
        allowed, _ = await limiter.check("xyz_route", "10.10.10.10")
        assert allowed is False

    @pytest.mark.asyncio
    async def test_validator_custom_malicious_patterns(self) -> None:
        custom = [(r"TOP SECRET", "Classified content")]
        validator = UploadValidator(malicious_patterns=custom)
        result = await validator.validate(
            "memo.txt", b"This is TOP SECRET info", "text/plain"
        )
        assert not result.ok
        assert result.status == ValidationStatus.MALICIOUS_CONTENT

    @pytest.mark.asyncio
    async def test_validator_allowed_mimes_custom(self) -> None:
        validator = UploadValidator(allowed_mimes={"text/plain"})
        result = await validator.validate("doc.pdf", b"%PDF-fake", "application/pdf")
        assert not result.ok
        assert result.status == ValidationStatus.MIME_NOT_ALLOWED
