"""Search, report retrieval, and statistics API using Elasticsearch.

Endpoints:
  - POST /api/search      — Existing semantic search (TF-IDF placeholder)
  - GET  /api/search      — Full-text search via ES 'reports' index
  - GET  /api/report/{id} — Single report from ES
  - GET  /api/stats       — Aggregated counts from ES
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from httpx import AsyncClient, HTTPError
from pydantic import BaseModel, Field

router = APIRouter(tags=["search"])

# ── ES client helper ────────────────────────────────────────────────────────────

ES_BASE = "http://localhost:9200"
REPORTS_INDEX = "reports"
TIMEOUT = 10.0


async def es_get(path: str, params: dict | None = None) -> dict | None:
    """Make a GET request to ES, returning None on connection errors."""
    try:
        async with AsyncClient(base_url=ES_BASE, timeout=TIMEOUT) as client:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
    except (HTTPError, OSError):
        return None


async def es_post(path: str, json_body: dict) -> dict | None:
    """Make a POST request to ES, returning None on connection errors."""
    try:
        async with AsyncClient(base_url=ES_BASE, timeout=TIMEOUT) as client:
            resp = await client.post(path, json=json_body)
            resp.raise_for_status()
            return resp.json()
    except (HTTPError, OSError):
        return None


# ── Schemas for existing POST /api/search ──────────────────────────────────────


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


# ── Schemas for new endpoints ──────────────────────────────────────────────────


class ESReportOut(BaseModel):
    """Single report document from Elasticsearch."""

    id: str
    enterprise: Optional[str] = None
    content: Optional[str] = None
    products: Optional[str] = None
    raw_materials: Optional[str] = None
    report_date: Optional[str] = None
    province: Optional[str] = None
    city: Optional[str] = None
    file_type: Optional[str] = None
    file_hash: Optional[str] = None
    ocr_status: Optional[str] = None
    highlights: Optional[dict[str, list[str]]] = None


class SearchHit(BaseModel):
    """A single hit in the full-text search response."""

    id: str
    score: float
    source: ESReportOut


class FullTextSearchResponse(BaseModel):
    """Response for GET /api/search."""

    total: int
    page: int
    size: int
    hits: list[SearchHit]
    took_ms: int = 0


class StatsResponse(BaseModel):
    """Response for GET /api/stats."""

    by_province: dict[str, int]
    by_ocr_status: dict[str, int]
    by_file_type: dict[str, int]


# ── Existing POST /api/search (semantic search, keep as-is) ───────────────────

@router.post("/api/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """Semantic search over environmental report corpus using TF-IDF + cosine similarity."""
    import time

    start = time.monotonic()

    try:
        from src.models.report import Report as ReportModel
    except ImportError:
        return SearchResponse(query=req.query, results=[], total=0)

    results: list[SearchResultItem] = []

    time_ms = (time.monotonic() - start) * 1000
    return SearchResponse(
        query=req.query,
        results=results,
        total=len(results),
        time_ms=round(time_ms, 1),
    )


# ── New GET /api/search (full-text search via ES) ────────────────────────────

@router.get("/api/search", response_model=FullTextSearchResponse)
async def fulltext_search(
    q: str = Query(..., min_length=1, description="Search query for full-text search"),
    enterprise: Optional[str] = Query(None, description="Filter by enterprise name (keyword)"),
    province: Optional[str] = Query(None, description="Filter by province (keyword)"),
    date_from: Optional[str] = Query(None, description="Start date (inclusive), format: YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="End date (inclusive), format: YYYY-MM-DD"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    size: int = Query(20, ge=1, le=100, description="Results per page"),
) -> FullTextSearchResponse:
    """Full-text search across the 'reports' ES index with optional filters."""

    # Build ES query
    must_clauses: list[dict] = [
        {
            "match": {
                "content": {
                    "query": q,
                    "analyzer": "ik_max_word",
                }
            }
        }
    ]

    filter_clauses: list[dict] = []
    if enterprise:
        filter_clauses.append({"term": {"enterprise": enterprise}})
    if province:
        filter_clauses.append({"term": {"province": province}})
    if date_from or date_to:
        range_clause: dict[str, Any] = {}
        if date_from:
            range_clause["gte"] = date_from
        if date_to:
            range_clause["lte"] = date_to
        filter_clauses.append({"range": {"report_date": range_clause}})

    es_query: dict[str, Any] = {
        "query": {
            "bool": {
                "must": must_clauses,
                "filter": filter_clauses,
            }
        },
        "from": (page - 1) * size,
        "size": size,
        "highlight": {
            "fields": {
                "content": {
                    "fragment_size": 150,
                    "number_of_fragments": 3,
                }
            }
        },
    }

    es_response = await es_post(f"/{REPORTS_INDEX}/_search", es_query)

    if es_response is None:
        # ES unavailable — return empty gracefully
        return FullTextSearchResponse(total=0, page=page, size=size, hits=[], took_ms=0)

    took_ms = es_response.get("took", 0)
    total_hits = es_response.get("hits", {}).get("total", {}).get("value", 0)
    hits_raw = es_response.get("hits", {}).get("hits", [])

    hits: list[SearchHit] = []
    for hit in hits_raw:
        src = hit.get("_source", {})
        highlight = hit.get("highlight", {})

        report_out = ESReportOut(
            id=hit["_id"],
            enterprise=src.get("enterprise"),
            content=src.get("content"),
            products=src.get("products"),
            raw_materials=src.get("raw_materials"),
            report_date=src.get("report_date"),
            province=src.get("province"),
            city=src.get("city"),
            file_type=src.get("file_type"),
            file_hash=src.get("file_hash"),
            ocr_status=src.get("ocr_status"),
            highlights=highlight if highlight else None,
        )

        hits.append(SearchHit(id=hit["_id"], score=hit["_score"], source=report_out))

    return FullTextSearchResponse(
        total=total_hits,
        page=page,
        size=size,
        hits=hits,
        took_ms=took_ms,
    )


# ── GET /api/report/{id} (single report from ES) ─────────────────────────────

@router.get("/api/report/{report_id}", response_model=ESReportOut)
async def get_report(report_id: str) -> ESReportOut:
    """Get a single report document from the ES 'reports' index by its _id."""
    es_response = await es_get(f"/{REPORTS_INDEX}/_doc/{report_id}")

    if es_response is None:
        raise HTTPException(status_code=503, detail="Elasticsearch is unavailable")

    if not es_response.get("found"):
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")

    src = es_response.get("_source", {})
    return ESReportOut(
        id=report_id,
        enterprise=src.get("enterprise"),
        content=src.get("content"),
        products=src.get("products"),
        raw_materials=src.get("raw_materials"),
        report_date=src.get("report_date"),
        province=src.get("province"),
        city=src.get("city"),
        file_type=src.get("file_type"),
        file_hash=src.get("file_hash"),
        ocr_status=src.get("ocr_status"),
    )


# ── GET /api/stats (aggregated counts from ES) ───────────────────────────────

@router.get("/api/stats", response_model=StatsResponse)
async def stats() -> StatsResponse:
    """Return total document counts grouped by province, ocr_status, and file_type."""
    aggs_body: dict[str, Any] = {
        "size": 0,
        "aggs": {
            "by_province": {"terms": {"field": "province", "size": 100}},
            "by_ocr_status": {"terms": {"field": "ocr_status", "size": 20}},
            "by_file_type": {"terms": {"field": "file_type", "size": 20}},
        },
    }

    es_response = await es_post(f"/{REPORTS_INDEX}/_search", aggs_body)

    if es_response is None:
        return StatsResponse(by_province={}, by_ocr_status={}, by_file_type={})

    def extract_buckets(agg_key: str) -> dict[str, int]:
        buckets = (
            es_response.get("aggregations", {})
            .get(agg_key, {})
            .get("buckets", [])
        )
        return {b["key"]: b["doc_count"] for b in buckets}

    return StatsResponse(
        by_province=extract_buckets("by_province"),
        by_ocr_status=extract_buckets("by_ocr_status"),
        by_file_type=extract_buckets("by_file_type"),
    )


# ── Existing GET /api/search/suggest (keep as-is) ────────────────────────────

@router.get("/api/search/suggest")
async def suggest(
    q: str = Query(..., min_length=1, description="Partial query for autocomplete"),
    limit: int = Query(5, ge=1, le=20),
) -> list[str]:
    """Autocomplete suggestions for search queries."""
    return []
