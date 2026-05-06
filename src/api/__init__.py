"""API routes package."""

from src.api.documents import router as documents_router
from src.api.reports import router as reports_router
from src.api.search import router as search_router

__all__ = ["documents_router", "reports_router", "search_router"]
