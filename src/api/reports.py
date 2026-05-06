"""Reports API — list, filter, and retrieve crawled environmental reports."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/reports", tags=["reports"])


# ── Schemas ────────────────────────────────────────────────────────────────────


class ReportOut(BaseModel):
    id: int
    url: str
    title: Optional[str] = None
    source: Optional[str] = None
    file_type: Optional[str] = None
    file_url: Optional[str] = None
    published_at: Optional[datetime] = None
    scraped_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ReportListResponse(BaseModel):
    items: list[ReportOut]
    total: int
    page: int
    page_size: int


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("", response_model=ReportListResponse)
async def list_reports(
    source: Optional[str] = Query(None, description="Filter by source: mee, cninfo"),
    file_type: Optional[str] = Query(None, description="Filter by file type: pdf, docx"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ReportListResponse:
    """List crawled environmental reports with optional filters."""
    # Placeholder — in production, query PostgreSQL via SQLAlchemy
    return ReportListResponse(items=[], total=0, page=page, page_size=page_size)


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(report_id: int) -> ReportOut:
    """Get a single report by ID."""
    raise HTTPException(status_code=404, detail="Report not found")


@router.get("/stats/summary")
async def report_stats() -> dict:
    """Aggregate statistics about the report corpus."""
    return {
        "total_reports": 0,
        "by_source": {"mee": 0, "cninfo": 0},
        "by_file_type": {},
        "date_range": {"earliest": None, "latest": None},
    }
