"""Centralized application configuration.

Reads from environment variables with sensible defaults.
Use .env file or Docker Compose environment for overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Settings:
    """Application settings with env-var defaults."""

    # ═══ App ═══
    app_name: str = field(default_factory=lambda: os.getenv("APP_NAME", "Environmental Reports Platform"))
    app_version: str = field(default_factory=lambda: os.getenv("APP_VERSION", "0.2.0"))
    debug: bool = field(default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true")

    # ═══ Security ═══
    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", "dev-secret-change-in-production"))
    jwt_algorithm: str = field(default_factory=lambda: os.getenv("JWT_ALGORITHM", "HS256"))
    access_token_expire_minutes: int = field(default_factory=lambda: int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")))
    refresh_token_expire_days: int = field(default_factory=lambda: int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7")))

    # ═══ Database ═══
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/environmental"
    ))

    # ═══ Redis ═══
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))

    # ═══ Elasticsearch ═══
    elasticsearch_url: str = field(default_factory=lambda: os.getenv("ELASTICSEARCH_URL", "http://localhost:9200"))

    # ═══ MinIO ═══
    minio_endpoint: str = field(default_factory=lambda: os.getenv("MINIO_ENDPOINT", "localhost:9000"))
    minio_access_key: str = field(default_factory=lambda: os.getenv("MINIO_ACCESS_KEY", "minioadmin"))
    minio_secret_key: str = field(default_factory=lambda: os.getenv("MINIO_SECRET_KEY", "minioadmin"))
    minio_bucket: str = field(default_factory=lambda: os.getenv("MINIO_BUCKET", "environmental-reports"))
    minio_secure: bool = field(default_factory=lambda: os.getenv("MINIO_SECURE", "false").lower() == "true")

    # ═══ Upload ═══
    upload_dir: Path = field(default_factory=lambda: Path(os.getenv("UPLOAD_DIR", "/tmp/environmental_uploads")))
    max_upload_size_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_SIZE_MB", "50")))

    # ═══ Crawler ═══
    crawler_cache_dir: Path = field(default_factory=lambda: Path(os.getenv("CRAWLER_CACHE_DIR", "/tmp/environmental_crawler_cache")))
    crawler_download_delay: float = field(default_factory=lambda: float(os.getenv("CRAWLER_DOWNLOAD_DELAY", "0.5")))

    # ═══ AI ═══
    deepseek_api_key: Optional[str] = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY"))
    deepseek_base_url: str = field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))

    # ═══ Logging ═══
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    def __post_init__(self):
        """Ensure directories exist."""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.crawler_cache_dir.mkdir(parents=True, exist_ok=True)


# Global singleton
settings = Settings()
