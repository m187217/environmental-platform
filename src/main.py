"""FastAPI application entry point — Environmental Reports Platform."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import documents_router, reports_router, search_router
from src.security.middleware import SecurityMiddleware
from src.user.routes import router as auth_router

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Lifespan ───────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Environmental Platform starting up...")
    # Try to initialize DB (non-fatal if not available)
    try:
        from src.models.database import init_db
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning("Database not available (running in dev mode): %s", e)
    yield
    logger.info("Environmental Platform shutting down...")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Environmental Reports Platform",
    description="中国企业环境评估报告共享与匹配平台",
    version="0.2.0",
    lifespan=lifespan,
)

# ── Security Middleware (bot detection + rate limiting) ───────────────────────

app.add_middleware(
    SecurityMiddleware,
    exclude_paths={"/health", "/metrics", "/favicon.ico", "/docs", "/openapi.json", "/redoc"},
)

# ── CORS ───────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ─────────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(reports_router)
app.include_router(search_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.2.0"}


@app.get("/")
async def root() -> dict:
    return {
        "name": "Environmental Reports Platform",
        "version": "0.2.0",
        "docs": "/docs",
    }
