"""Sensitive data sanitizer for environmental documents.

Masks personally identifiable information (PII) from document text:
- Chinese phone numbers (mobile and landline)
- Chinese ID card numbers (18-digit)
- Email addresses
- Bank card numbers (basic patterns)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MaskedMatch:
    """A match that was masked in the text."""

    original: str
    masked: str
    category: str
    start: int
    end: int


@dataclass
class SanitizerResult:
    """Result of text sanitization."""

    sanitized_text: str
    masks_applied: list[MaskedMatch] = field(default_factory=list)
    original_length: int = 0

    @property
    def masks_count(self) -> int:
        return len(self.masks_applied)


# ── PII patterns ────────────────────────────────────────────────────────────

# Chinese mobile phone: 1[3-9]xxxxxxxxx, optionally with +86 or spaces
_CHINESE_MOBILE_RE = re.compile(
    r"(?:(?:\+?86)?[ \t]*)?1[3-9]\d[ \t]?\d{4}[ \t]?\d{4}"
)

# Chinese landline: 0XXX-XXXXXXX or 0XXX-XXXXXXXX (with optional area code parens)
_CHINESE_LANDLINE_RE = re.compile(
    r"(?:\(?0\d{2,3}\)?[ \t\-]?)?\d{7,8}"
)

# Combined phone pattern
_PHONE_RE = re.compile(
    r"(?:(?:\+?86)?[ \t]*1[3-9]\d[ \t]?\d{4}[ \t]?\d{4})"
    r"|(?:\(?0\d{2,3}\)?[ \t\-]?\d{7,8})"
)

# Chinese 18-digit ID card number (with check pattern for birth date)
_ID_CARD_RE = re.compile(
    r"[1-9]\d{5}"  # 6-digit area code
    r"(?:19|20)\d{2}"  # Year 1900-2099
    r"(?:0[1-9]|1[0-2])"  # Month 01-12
    r"(?:0[1-9]|[12]\d|3[01])"  # Day 01-31
    r"\d{3}"  # Sequence
    r"[\dXx]"  # Check digit
)

# Email addresses
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# Bank card numbers (16-19 digits, often space-separated)
_BANK_CARD_RE = re.compile(
    r"\b\d{4}[ \t]?\d{4}[ \t]?\d{4}[ \t]?\d{4}[ \t]?\d{0,3}\b"
)


# ── Masking functions ────────────────────────────────────────────────────────


def _mask_phone(match: re.Match[str]) -> str:
    """Mask a phone number, keeping first 3 and last 4 digits."""
    digits = re.sub(r"\D", "", match.group())
    if len(digits) >= 11:  # mobile
        return digits[:3] + "****" + digits[-4:]
    elif len(digits) >= 10:  # landline
        return digits[:3] + "****" + digits[-3:]
    else:
        return "****"


def _mask_id_card(match: re.Match[str]) -> str:
    """Mask an ID card, keeping first 6 and last 4 digits."""
    digits = match.group()
    if len(digits) >= 15:
        return digits[:6] + "********" + digits[-4:]
    return "****"


def _mask_email(match: re.Match[str]) -> str:
    """Mask an email, keeping first char and domain."""
    email = match.group()
    parts = email.split("@")
    if len(parts) == 2:
        return parts[0][:1] + "***@" + parts[1]
    return "***@***"


def _mask_bank_card(match: re.Match[str]) -> str:
    """Mask a bank card, keeping last 4 digits."""
    digits = re.sub(r"\D", "", match.group())
    if len(digits) >= 12:
        return "****" + digits[-4:]
    return "****"


# ── Category definitions ─────────────────────────────────────────────────────

_SANITIZE_RULES: list[tuple[str, re.Pattern[str], callable]] = [
    ("phone", _PHONE_RE, _mask_phone),
    ("id_card", _ID_CARD_RE, _mask_id_card),
    ("email", _EMAIL_RE, _mask_email),
    ("bank_card", _BANK_CARD_RE, _mask_bank_card),
]


def sanitize(text: str, categories: list[str] | None = None) -> SanitizerResult:
    """Mask sensitive PII from text.

    Applies regex-based masking for phone numbers, ID cards, emails,
    and bank card numbers.

    Args:
        text: Input text to sanitize.
        categories: Optional list of categories to mask.
            If None, all categories are applied.
            Valid categories: "phone", "id_card", "email", "bank_card".

    Returns:
        SanitizerResult with sanitized text and list of applied masks.
    """
    if not text:
        return SanitizerResult(sanitized_text="", original_length=0)

    result = text
    masks: list[MaskedMatch] = []

    valid_categories = {cat for cat, _, _ in _SANITIZE_RULES}
    if categories is not None:
        invalid = set(categories) - valid_categories
        if invalid:
            raise ValueError(
                f"Invalid sanitization categories: {invalid}. "
                f"Valid: {valid_categories}"
            )
        active_rules = [
            (cat, pattern, mask_fn)
            for cat, pattern, mask_fn in _SANITIZE_RULES
            if cat in categories
        ]
    else:
        active_rules = list(_SANITIZE_RULES)

    # Collect all matches first (to avoid offset issues from in-place replacement)
    all_matches: list[tuple[int, int, str, str, str]] = []

    for category, pattern, mask_fn in active_rules:
        for match in pattern.finditer(result):
            masked = mask_fn(match)
            all_matches.append(
                (match.start(), match.end(), match.group(), masked, category)
            )

    # Sort by start position descending to replace from end to start
    all_matches.sort(key=lambda m: m[0], reverse=True)

    # Apply replacements
    result_list = list(result)
    for start, end, original, masked, category in all_matches:
        result_list[start:end] = masked
        masks.append(
            MaskedMatch(
                original=original,
                masked=masked,
                category=category,
                start=start,
                end=start + len(masked),
            )
        )

    # Restore chronological order for the masks list
    masks.reverse()

    return SanitizerResult(
        sanitized_text="".join(result_list),
        masks_applied=masks,
        original_length=len(text),
    )


def sanitize_file_content(text: str) -> str:
    """Convenience: sanitize text and return only the sanitized string.

    Args:
        text: Input text to sanitize.

    Returns:
        Sanitized text string.
    """
    return sanitize(text).sanitized_text


def is_pii_present(text: str) -> bool:
    """Check if text contains any PII patterns.

    Args:
        text: Input text to check.

    Returns:
        True if any PII patterns are detected.
    """
    for _, pattern, _ in _SANITIZE_RULES:
        if pattern.search(text):
            return True
    return False
