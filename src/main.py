"""FastAPI application entry point — Environmental Reports Platform."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import documents_router, reports_router, search_router
from src.config import settings
from src.security.middleware import SecurityMiddleware
from src.user.routes import router as auth_router

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Lifespan ───────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("%s v%s starting up...", settings.app_name, settings.app_version)

    # Initialize database (non-fatal in dev mode)
    try:
        from src.models.database import init_db
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning("Database not available (running in dev mode): %s", e)

    # Ensure upload directory
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Upload dir: %s", settings.upload_dir)

    yield

    logger.info("%s shutting down...", settings.app_name)


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    description="中国企业环境评估报告共享与匹配平台",
    version=settings.app_version,
    lifespan=lifespan,
    debug=settings.debug,
)

# ── Security Middleware ────────────────────────────────────────────────────────

app.add_middleware(
    SecurityMiddleware,
    exclude_paths={"/health", "/status", "/metrics", "/favicon.ico", "/docs", "/openapi.json", "/redoc"},
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
    return {
        "status": "ok",
        "version": settings.app_version,
    }


@app.get("/status")
async def status() -> dict:
    """Extended status with external service health."""
    import time
    services = {}

    # Check DB
    try:
        from src.models.database import engine
        import asyncio
        async with asyncio.timeout(3):
            async with engine.connect() as conn:
                await conn.execute("SELECT 1")
        services["database"] = "ok"
    except Exception:
        services["database"] = "unavailable"

    # Check Redis
    try:
        import redis.asyncio as aioredis
        import asyncio
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await asyncio.wait_for(r.ping(), timeout=3)
        await r.close()
        services["redis"] = "ok"
    except Exception:
        services["redis"] = "unavailable"

    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "services": services,
    }


@app.get("/")
async def root() -> dict:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "status": "/status",
        "health": "/health",
    }
