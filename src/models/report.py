"""Report data model for crawled environmental documents."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class Report(Base):
    """Stores metadata for crawled environmental reports."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(String(64))  # 'mee', 'cninfo'
    section: Mapped[Optional[str]] = mapped_column(String(64))
    file_type: Mapped[Optional[str]] = mapped_column(String(16))  # 'pdf', 'docx', etc.
    file_url: Mapped[Optional[str]] = mapped_column(Text)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    raw_meta: Mapped[Optional[dict]] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_reports_source", "source"),
        Index("ix_reports_published_at", "published_at"),
        Index("ix_reports_scraped_at", "scraped_at"),
        Index("ix_reports_file_type", "file_type"),
    )
