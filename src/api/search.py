"""Semantic search API over environmental report embeddings."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.processor.embedder import compute_similarity

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="Search query text")
    max_features: int = Field(5000, ge=100, le=50000)
    top_k: int = Field(10, ge=1, le=100)


class SearchResultItem(BaseModel):
    report_id: Optional[str] = None
    title: str
    source: str
    score: float
    snippet: str = ""


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]
    total: int
    time_ms: float = 0.0


@router.post("", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """Semantic search over environmental report corpus using TF-IDF + cosine similarity."""
    import time

    start = time.monotonic()

    # For now, use in-memory document store (mocked for MVP)
    # In production, query Elasticsearch with pre-computed embeddings
    try:
        from src.models.report import Report as ReportModel
    except ImportError:
        # Return empty result if models aren't ready
        return SearchResponse(query=req.query, results=[], total=0)

    # Placeholder — will integrate with Elasticsearch in production
    results: list[SearchResultItem] = []
    
    time_ms = (time.monotonic() - start) * 1000
    return SearchResponse(
        query=req.query,
        results=results,
        total=len(results),
        time_ms=round(time_ms, 1),
    )


@router.get("/suggest")
async def suggest(
    q: str = Query(..., min_length=1, description="Partial query for autocomplete"),
    limit: int = Query(5, ge=1, le=20),
) -> list[str]:
    """Autocomplete suggestions for search queries."""
    # Placeholder for Elasticsearch completion suggester
    return []
