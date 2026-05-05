"""Scrapy settings for the Environmental Platform crawler.

Configures Playwright browser automation, anti-blocking middleware, rate
limiting, and structured logging for scraping Chinese government websites.
"""

import os
from pathlib import Path

# ── Spider Metadata ──────────────────────────────────────────────────────────

BOT_NAME = "environmental_crawler"
SPIDER_MODULES = ["src.crawler.spiders"]
NEWSPIDER_MODULE = "src.crawler.spiders"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

ROBOTSTXT_OBEY = False  # Chinese gov sites often block via robots.txt
COOKIES_ENABLED = True

# ── Concurrency & Rate Limiting ──────────────────────────────────────────────

CONCURRENT_REQUESTS = 4          # Conservative for government servers
CONCURRENT_REQUESTS_PER_DOMAIN = 2
CONCURRENT_REQUESTS_PER_IP = 2

DOWNLOAD_DELAY = 0.35            # ~3 req/s ceiling per domain
RANDOMIZE_DOWNLOAD_DELAY = True  # Jitter: 0.5x–1.5x delay

# ── Retry & Backoff ──────────────────────────────────────────────────────────

RETRY_ENABLED = True
RETRY_TIMES = 5
RETRY_HTTP_CODES = [429, 500, 502, 503, 504, 408, 403]

# Exponential backoff: base delay roughly doubles each retry
RETRY_PRIORITY_ADJUST = -1       # Deprioritize retries
DOWNLOAD_TIMEOUT = 45

# ── Middleware Pipeline ──────────────────────────────────────────────────────

DOWNLOADER_MIDDLEWARES = {
    "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
    "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
    "scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware": 750,
    "src.crawler.middleware.UserAgentRotationMiddleware": 400,
    "src.crawler.middleware.ThrottleMiddleware": 410,
    "src.crawler.middleware.RateLimitRetryMiddleware": 420,
}

# scrapy-playwright integration (optional — enable in spiders that need it)
# Install: pip install scrapy-playwright
try:
    import scrapy_playwright  # noqa: F401

    DOWNLOAD_HANDLERS = {
        "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    }
    TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

    PLAYWRIGHT_BROWSER_TYPE = "chromium"
    PLAYWRIGHT_LAUNCH_OPTIONS = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    }
    PLAYWRIGHT_CONTEXTS = {
        "default": {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        },
    }
    PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 30_000  # ms
except ImportError:
    PLAYWRIGHT_BROWSER_TYPE = None

# ── Item Pipelines ───────────────────────────────────────────────────────────

ITEM_PIPELINES = {
    "src.crawler.pipelines.DeduplicationPipeline": 100,
    "src.crawler.pipelines.DatabasePipeline": 200,
}

# ── Database ─────────────────────────────────────────────────────────────────

# PostgreSQL connection (override via env vars)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://crawler:crawler@localhost:5432/environmental",
)

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("CRAWLER_LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"

# structlog integration via Scrapy extension
EXTENSIONS = {
    "src.crawler.middleware.StructlogLogging": 0,
}

# ── Feeds / Export ───────────────────────────────────────────────────────────

FEED_EXPORT_ENCODING = "utf-8"

# Cache directory for dedup
CRAWLER_CACHE_DIR = Path(
    os.getenv("CRAWLER_CACHE_DIR", "/tmp/environmental_crawler_cache")
)
CRAWLER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── Proxy Configuration ──────────────────────────────────────────────────────

# Set via env: PROXY_LIST=http://proxy1,http://proxy2
PROXY_LIST = os.getenv("PROXY_LIST", "").split(",") if os.getenv("PROXY_LIST") else []
PROXY_ROTATION_INTERVAL = int(os.getenv("PROXY_ROTATION_INTERVAL", "300"))  # 5 min

# ── Scraping Behaviour ───────────────────────────────────────────────────────

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0
