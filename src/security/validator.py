"""File upload validation — MIME check, size limit, malicious pattern scan.

Usage:
    from src.security import UploadValidator

    validator = UploadValidator(max_size_mb=50)
    result = await validator.validate(filename, content, content_type)
    if not result.ok:
        raise HTTPException(422, detail=result.error_message)
"""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed MIME types (whitelist)
# ---------------------------------------------------------------------------

ALLOWED_MIME_TYPES: set[str] = {
    # Documents
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # Plain text
    "text/plain",
    "text/csv",
    "text/html",
    "application/json",
    "application/xml",
    "text/xml",
    # Images
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    # Archives
    "application/zip",
    "application/x-rar-compressed",
    "application/gzip",
    "application/x-tar",
    # Spreadsheets
    "text/tab-separated-values",
}

# Extensions mapped to MIME (strengthen weak system MIME db)
_EXTENSION_MIME_OVERRIDES: dict[str, str] = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}

# ---------------------------------------------------------------------------
# Malicious pattern scanner
# ---------------------------------------------------------------------------

# Regex patterns that signal potentially malicious file content.
# In production these would be much more comprehensive.
_MALICIOUS_PATTERNS: list[tuple[str, str]] = [
    # PHP / server-side script injection
    (r"<\?(?:php|=)", "PHP open tag detected"),
    # JavaScript event handlers (XSS)
    (r"\bon\w+\s*=\s*[\"']?\s*javascript:", "Inline JS event handler"),
    # Embedded scripts
    (r"<script\b", "Embedded <script> tag"),
    # Shell commands
    (r"(?:^|\n)\s*(?:/bin/(?:ba|z)?sh|/bin/bash|/usr/bin/)\s*\S*", "Shell shebang"),
    # SQL injection probe patterns
    (
        r"(?i)(?:union\s+select|/\*!|\bOR\s+['\"]?\d['\"]?\s*=\s*['\"]?\d)",
        "SQL injection probe",
    ),
    # Base64-encoded payloads > 200 chars (often used in exploits)
    # (checked procedurally, not via regex — see below)
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class ValidationStatus(str, Enum):
    OK = "ok"
    SIZE_EXCEEDED = "size_exceeded"
    MIME_NOT_ALLOWED = "mime_not_allowed"
    EXTENSION_MISMATCH = "extension_mismatch"
    MALICIOUS_CONTENT = "malicious_content"
    EMPTY_FILE = "empty_file"


@dataclass
class ValidationResult:
    """Result of a file upload validation."""

    status: ValidationStatus
    filename: str = ""
    detected_mime: str | None = None
    file_size_bytes: int = 0
    error_message: str = ""
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == ValidationStatus.OK


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class UploadValidator:
    """Validates file uploads for security and policy compliance.

    Checks performed (in order):
        1. File is not empty.
        2. File size ≤ *max_size_mb*.
        3. MIME type is in the allowed whitelist.
        4. Extension-to-MIME consistency (optional, enabled by default).
        5. Content scan for malicious patterns.

    Args:
        max_size_mb: Maximum allowed file size in megabytes (default: 50).
        allowed_mimes: Custom set of allowed MIME types.  When ``None`` the
            built-in whitelist (:data:`ALLOWED_MIME_TYPES`) is used.
        malicious_patterns: Custom list of ``(regex, description)`` tuples
            for content scanning.
        check_extension: Validate that the file extension matches the
            claimed or detected MIME type.
    """

    MAX_SIZE_BYTES_DEFAULT: ClassVar[int] = 50 * 1024 * 1024  # 50 MB

    def __init__(
        self,
        max_size_mb: int = 50,
        allowed_mimes: set[str] | None = None,
        malicious_patterns: list[tuple[str, str]] | None = None,
        check_extension: bool = True,
    ) -> None:
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._allowed_mimes = allowed_mimes or ALLOWED_MIME_TYPES
        self._malicious_patterns = malicious_patterns or _MALICIOUS_PATTERNS
        self._check_extension = check_extension

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def validate(
        self,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> ValidationResult:
        """Run the full validation pipeline.

        Args:
            filename: Original filename (used for extension inspection).
            content: Raw file bytes.
            content_type: The ``Content-Type`` header supplied by the client.

        Returns:
            A :class:`ValidationResult` whose ``.ok`` property indicates
            success.
        """
        file_size = len(content)

        # 1. Empty check
        if file_size == 0:
            return ValidationResult(
                status=ValidationStatus.EMPTY_FILE,
                filename=filename,
                file_size_bytes=0,
                error_message="Uploaded file is empty.",
            )

        # 2. Size check
        if file_size > self._max_size_bytes:
            size_mb = file_size / (1024 * 1024)
            limit_mb = self._max_size_bytes / (1024 * 1024)
            return ValidationResult(
                status=ValidationStatus.SIZE_EXCEEDED,
                filename=filename,
                file_size_bytes=file_size,
                error_message=(
                    f"File size {size_mb:.1f} MB exceeds "
                    f"maximum of {limit_mb:.1f} MB."
                ),
                details={"size_bytes": file_size, "limit_bytes": self._max_size_bytes},
            )

        # 3. MIME whitelist check
        effective_mime = self._resolve_mime(filename, content, content_type)
        if effective_mime not in self._allowed_mimes:
            return ValidationResult(
                status=ValidationStatus.MIME_NOT_ALLOWED,
                filename=filename,
                detected_mime=effective_mime,
                file_size_bytes=file_size,
                error_message=f"MIME type '{effective_mime}' is not allowed.",
                details={"detected_mime": effective_mime},
            )

        # 4. Extension consistency (optional)
        if self._check_extension:
            ext_mime = self._guess_mime_from_extension(filename)
            if ext_mime and ext_mime != effective_mime:
                return ValidationResult(
                    status=ValidationStatus.EXTENSION_MISMATCH,
                    filename=filename,
                    detected_mime=effective_mime,
                    file_size_bytes=file_size,
                    error_message=(
                        f"MIME type '{effective_mime}' does not match "
                        f"file extension (expected '{ext_mime}')."
                    ),
                    details={
                        "declared_mime": effective_mime,
                        "extension_mime": ext_mime,
                    },
                )

        # 5. Malicious content scan (text-based files only)
        if self._is_text_mime(effective_mime):
            scan_result = await self._scan_content(content)
            if scan_result:
                return ValidationResult(
                    status=ValidationStatus.MALICIOUS_CONTENT,
                    filename=filename,
                    detected_mime=effective_mime,
                    file_size_bytes=file_size,
                    error_message=f"Suspicious content detected: {scan_result}",
                    details={"match": scan_result},
                )

        logger.debug("Upload validation passed", extra={"filename": filename})
        return ValidationResult(
            status=ValidationStatus.OK,
            filename=filename,
            detected_mime=effective_mime,
            file_size_bytes=file_size,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_mime(
        self, filename: str, content: bytes, content_type: str | None
    ) -> str:
        """Derive the best MIME type from available signals."""
        # 1. Trust explicit Content-Type if present and ianatized
        if content_type and "/" in content_type:
            return content_type.split(";")[0].strip().lower()

        # 2. Guess from extension (including overrides)
        ext_mime = self._guess_mime_from_extension(filename)
        if ext_mime:
            return ext_mime

        # 3. Magic-byte inspection (lightweight; python-magic optional)
        magic_mime = self._guess_mime_from_magic(content)
        if magic_mime:
            return magic_mime

        return "application/octet-stream"

    @staticmethod
    def _guess_mime_from_extension(filename: str) -> str | None:
        """Guess MIME from filename extension."""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if not ext:
            return None
        dotted = f".{ext}"
        if dotted in _EXTENSION_MIME_OVERRIDES:
            return _EXTENSION_MIME_OVERRIDES[dotted]
        mime, _ = mimetypes.guess_type(filename)
        return mime

    @staticmethod
    def _guess_mime_from_magic(content: bytes) -> str | None:
        """Inspect magic bytes for common file types (no external deps)."""
        if len(content) < 4:
            return None
        if content.startswith(b"\x89PNG"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"GIF8"):
            return "image/gif"
        if content.startswith(b"%PDF"):
            return "application/pdf"
        if content.startswith(b"PK\x03\x04"):
            return "application/zip"
        if content[:4] == b"\xd0\xcf\x11\xe0":
            return "application/msword"  # OLE2 — .doc / .xls
        return None

    @staticmethod
    def _is_text_mime(mime: str) -> bool:
        """Return True if *mime* can reasonably be scanned as text."""
        return mime.startswith("text/") or mime in {
            "application/json",
            "application/xml",
            "text/xml",
            "application/javascript",
        }

    async def _scan_content(self, content: bytes) -> str | None:
        """Scan text content for malicious patterns.

        Returns the description of the first match, or ``None``.
        """
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            # If we can't decode, treat as binary — skip pattern scan
            return None

        for pattern, description in self._malicious_patterns:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                logger.warning(
                    "Malicious pattern detected",
                    extra={"pattern": pattern, "description": description},
                )
                return description

        # Long base64 string heuristic (> 300 contiguous base64 chars)
        if re.search(r"[A-Za-z0-9+/=]{300,}", text):
            return "Suspiciously long Base64 string"

        return None
