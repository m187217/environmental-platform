"""Item pipelines for the environmental crawler.

Handles:
  - URL-hash deduplication (prevents re-scraping identical reports)
  - Database persistence via async PostgreSQL
  - Timestamp tracking for incremental crawls
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import scrapy
import structlog
from scrapy import Spider
from scrapy.settings import Settings

logger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Deduplication Pipeline
# ═══════════════════════════════════════════════════════════════════════════════


class DeduplicationPipeline:
    """Deduplicates items by SHA-256 hash of their source URL.

    Maintains a local cache file of seen URL hashes so dedup survives
    crawler restarts. Also keeps an in-memory set for fast lookups.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._cache_file = cache_dir / "url_hashes.json"
        self._seen_hashes: set[str] = self._load_cache()

    @classmethod
    def from_crawler(cls, crawler: scrapy.crawler.Crawler) -> "DeduplicationPipeline":
        cache_dir = Path(
            crawler.settings.get("CRAWLER_CACHE_DIR", "/tmp/environmental_crawler_cache")
        )
        return cls(cache_dir)

    def _load_cache(self) -> set[str]:
        """Load previously seen URL hashes from the JSON cache file."""
        if not self._cache_file.exists():
            logger.info("dedup_cache_not_found", path=str(self._cache_file))
            return set()

        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            hashes = set(data.get("hashes", []))
            logger.info("dedup_cache_loaded", count=len(hashes))
            return hashes
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("dedup_cache_load_failed", error=str(exc))
            return set()

    def _save_cache(self) -> None:
        """Persist seen URL hashes to the JSON cache file."""
        try:
            payload = {
                "hashes": sorted(self._seen_hashes),
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            self._cache_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("dedup_cache_save_failed", error=str(exc))

    def _url_hash(self, url: str) -> str:
        """Compute SHA-256 hex digest of a URL string."""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def process_item(self, item: dict[str, Any], spider: Spider) -> Optional[dict[str, Any]]:
        """Drop the item if its URL has already been seen.

        Args:
            item: Scrapy item dict with at least a 'url' key.
            spider: The active spider instance.

        Returns:
            The item if new, or None (drop) if duplicate.
        """
        url = item.get("url")
        if not url:
            logger.warning("dedup_no_url", item_keys=list(item.keys()))
            return item  # Pass through if no URL to hash

        url_hash = self._url_hash(url)
        if url_hash in self._seen_hashes:
            logger.debug("dedup_dropped", url=url[:120])
            return None

        self._seen_hashes.add(url_hash)
        # Persist cache every 50 new items to reduce I/O
        if len(self._seen_hashes) % 50 == 0:
            self._save_cache()

        return item

    def close_spider(self, spider: Spider) -> None:
        """Flush the hash cache on spider close."""
        self._save_cache()
        logger.info("dedup_cache_saved", count=len(self._seen_hashes))


# ═══════════════════════════════════════════════════════════════════════════════
# Database Pipeline
# ═══════════════════════════════════════════════════════════════════════════════


class DatabasePipeline:
    """Persists scraped report metadata to PostgreSQL.

    Schema (reports table):
        id           SERIAL PRIMARY KEY
        url_hash     VARCHAR(64) UNIQUE NOT NULL  -- SHA-256 of URL
        url          TEXT NOT NULL
        title        TEXT
        source       VARCHAR(64)  -- 'mee' or 'cninfo'
        file_type    VARCHAR(16)  -- 'pdf', 'docx', 'xlsx', etc.
        file_url     TEXT         -- Direct download URL
        published_at TIMESTAMPTZ
        scraped_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        raw_meta     JSONB
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool = None
        self._items_buffer: list[dict[str, Any]] = []
        self._flush_size = 25  # Batch-insert every N items

    @classmethod
    def from_crawler(cls, crawler: scrapy.crawler.Crawler) -> "DatabasePipeline":
        database_url = crawler.settings.get(
            "DATABASE_URL",
            "postgresql+asyncpg://crawler:crawler@localhost:5432/environmental",
        )
        return cls(database_url)

    async def _ensure_pool(self) -> None:
        """Lazily create the asyncpg connection pool."""
        if self._pool is not None:
            return
        try:
            import asyncpg

            # Parse the connection string for asyncpg
            # Format: postgresql+asyncpg://user:pass@host:port/db
            dsn: str = self._database_url
            if dsn.startswith("postgresql+asyncpg://"):
                dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

            self._pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=2,
                max_size=8,
                command_timeout=30,
            )
            await self._create_table()
            logger.info("db_pool_created")
        except ImportError:
            logger.warning("asyncpg_not_installed_db_disabled")
            self._pool = None
        except Exception as exc:
            logger.error("db_pool_failed", error=str(exc))
            self._pool = None

    async def _create_table(self) -> None:
        """Ensure the reports table exists."""
        if self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id            SERIAL PRIMARY KEY,
                    url_hash      VARCHAR(64) UNIQUE NOT NULL,
                    url           TEXT NOT NULL,
                    title         TEXT,
                    source        VARCHAR(64),
                    file_type     VARCHAR(16),
                    file_url      TEXT,
                    published_at  TIMESTAMPTZ,
                    scraped_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    raw_meta      JSONB
                );
                CREATE INDEX IF NOT EXISTS idx_reports_url_hash ON reports(url_hash);
                CREATE INDEX IF NOT EXISTS idx_reports_source ON reports(source);
                CREATE INDEX IF NOT EXISTS idx_reports_published_at ON reports(published_at);
                CREATE INDEX IF NOT EXISTS idx_reports_scraped_at ON reports(scraped_at);
            """)
            logger.info("db_table_ensured")

    async def _flush_buffer(self) -> int:
        """Insert buffered items into the database.

        Uses INSERT ... ON CONFLICT to gracefully handle duplicates.

        Returns:
            Number of successfully inserted rows.
        """
        if not self._items_buffer or self._pool is None:
            return 0

        inserted = 0
        async with self._pool.acquire() as conn:
            for item in self._items_buffer:
                url = item.get("url", "")
                url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
                try:
                    await conn.execute(
                        """
                        INSERT INTO reports (url_hash, url, title, source, file_type,
                                             file_url, published_at, raw_meta)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (url_hash) DO NOTHING
                        """,
                        url_hash,
                        url,
                        item.get("title"),
                        item.get("source"),
                        item.get("file_type"),
                        item.get("file_url"),
                        item.get("published_at"),
                        json.dumps(item.get("raw_meta", {}), ensure_ascii=False),
                    )
                    inserted += 1
                except Exception as exc:
                    logger.error("db_insert_failed", url=url[:120], error=str(exc))

        self._items_buffer.clear()
        return inserted

    async def process_item(
        self, item: dict[str, Any], spider: Spider
    ) -> dict[str, Any]:
        """Buffer the item for batch insertion.

        Args:
            item: Scrapy item dict.
            spider: The active spider instance.

        Returns:
            The item, passed through to downstream pipelines.
        """
        await self._ensure_pool()

        if self._pool is not None:
            self._items_buffer.append(dict(item))
            if len(self._items_buffer) >= self._flush_size:
                count = await self._flush_buffer()
                logger.debug("db_flushed", count=count)

        return item

    async def close_spider(self, spider: Spider) -> None:
        """Flush remaining items and close the connection pool."""
        if self._items_buffer:
            count = await self._flush_buffer()
            logger.info("db_final_flush", count=count)
        if self._pool is not None:
            await self._pool.close()
            logger.info("db_pool_closed")
