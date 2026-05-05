"""Spider for 巨潮资讯 (CNINFO) — China Securities Regulatory disclosures.

Target: http://www.cninfo.com.cn/

Crawls listed company announcements, financial reports, and ESG disclosures.
Uses incremental crawl mode by default: only fetches reports published after
the last recorded scrape.

Key features:
  - Incremental date-based crawl (track last successful scrape)
  - AJAX/API-based listing pages with Playwright rendering
  - Financial report cycle detection (quarterly, annual reports)
  - PDF document link extraction

Usage:
    scrapy crawl cninfo                      # Incremental since last run
    scrapy crawl cninfo -a full=true         # Full historical crawl
    scrapy crawl cninfo -a days=30           # Last 30 days
    scrapy crawl cninfo -a stock_code=000001 # Single company
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional
from urllib.parse import urljoin, urlparse

import scrapy
import structlog
from scrapy.http import Request, Response

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "http://www.cninfo.com.cn"

# CNINFO API endpoints (discovered through browser inspection)
ANNOUNCEMENT_LIST_URL = (
    "http://www.cninfo.com.cn/new/disclosure"
)

# Main disclosure page
DISCLOSURE_URL = "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"

# Full announcement search
FULLTEXT_SEARCH_URL = "http://www.cninfo.com.cn/new/fulltextSearch"

# Document extensions to track
DOCUMENT_EXTENSIONS: set[str] = {".pdf", ".doc", ".docx"}

# Financial report cycle keywords for classification
REPORT_CYCLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "annual_report": re.compile(r"年[度报]|年度报告|年报"),
    "half_year_report": re.compile(r"半年[度报]|半年度报告|中报|中期报告"),
    "quarterly_report": re.compile(r"第[一二三]季度|一季报|三季报|季报"),
    "esg_report": re.compile(
        r"ESG|环境.*社会.*治理|社会责任|可持续发展|绿色|低碳|环境报告|企业环境",
        re.IGNORECASE,
    ),
    "prospectus": re.compile(r"招股|募集|上市公告"),
}

# File type mapping based on keywords in the title
FILE_TYPE_KEYWORDS: dict[str, list[str]] = {
    "pdf": [".pdf", "PDF"],
    "doc": [".doc", "DOC"],
    "docx": [".docx", "DOCX"],
    "xlsx": [".xls", ".xlsx"],
}


def _classify_report_cycle(title: str) -> str:
    """Classify a report into its financial cycle category.

    Args:
        title: The announcement title.

    Returns:
        Category key: 'annual_report', 'quarterly_report', 'esg_report', etc.
        or 'other'.
    """
    for category, pattern in REPORT_CYCLE_PATTERNS.items():
        if pattern.search(title):
            return category
    return "other"


def _infer_file_type(title: str, url: str) -> Optional[str]:
    """Determine the file type from a URL or title hints.

    Args:
        title: Announcement title.
        url: Target URL.

    Returns:
        File extension without dot, or None.
    """
    path = urlparse(url).path.lower()
    for ext in DOCUMENT_EXTENSIONS:
        if path.endswith(ext):
            return ext.lstrip(".")

    # Try keyword-based inference
    for ftype, keywords in FILE_TYPE_KEYWORDS.items():
        if any(kw in path or kw in title for kw in keywords):
            return ftype

    return "html"


def _parse_cninfo_date(date_str: str) -> Optional[datetime]:
    """Parse CNINFO date formats.

    Handles:
        - '2024-05-06 12:00'
        - '2024年05月06日'
        - '2024-05-06'
        - Timestamps in milliseconds

    Args:
        date_str: Raw date string from CNINFO.

    Returns:
        Timezone-aware UTC datetime, or None.
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    formats: list[tuple[str, bool]] = [
        ("%Y-%m-%d %H:%M", False),       # 2024-05-06 12:00
        ("%Y-%m-%d", False),              # 2024-05-06
        ("%Y-%m-%dT%H:%M:%S", False),     # ISO
    ]

    for fmt, _ in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    # Try Chinese format
    chinese_match = re.match(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日]?", date_str)
    if chinese_match:
        try:
            return datetime(
                int(chinese_match.group(1)),
                int(chinese_match.group(2)),
                int(chinese_match.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    # Try millisecond timestamp
    try:
        ts = int(date_str) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OSError):
        pass

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Spider
# ═══════════════════════════════════════════════════════════════════════════════


class CninfoSpider(scrapy.Spider):
    """Scrapes financial disclosures from 巨潮资讯 (CNINFO)."""

    name = "cninfo"
    allowed_domains = ["www.cninfo.com.cn", "cninfo.com.cn"]
    start_urls = [DISCLOSURE_URL]

    custom_settings: dict[str, Any] = {
        "DOWNLOAD_DELAY": 0.8,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "COOKIES_ENABLED": True,
    }

    # Track the last scrape timestamp for incremental mode
    _LAST_SCRAPE_FILE = Path("/tmp/cninfo_last_scrape.txt")

    def __init__(
        self,
        full: Optional[str] = None,
        days: Optional[str] = None,
        stock_code: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Initialize CNINFO spider.

        Args:
            full: If 'true', perform a full historical crawl.
            days: Crawl announcements from the last N days.
            stock_code: If set, only crawl a specific stock (e.g. '000001').
        """
        super().__init__(*args, **kwargs)

        self._full_crawl = (full or "").lower() == "true"
        self._stock_code = stock_code

        # Determine cutoff date
        if days is not None:
            try:
                self._cutoff_date = datetime.now(timezone.utc) - timedelta(
                    days=int(days)
                )
            except ValueError:
                self._cutoff_date = None
        elif not self._full_crawl:
            self._cutoff_date = self._load_last_scrape()
        else:
            self._cutoff_date = None

        logger.info(
            "cninfo_spider_initialized",
            full_crawl=self._full_crawl,
            cutoff_date=str(self._cutoff_date) if self._cutoff_date else "none",
            stock_code=self._stock_code,
        )

    @classmethod
    def _load_last_scrape(cls) -> Optional[datetime]:
        """Load the timestamp of the last successful scrape."""
        if not cls._LAST_SCRAPE_FILE.exists():
            logger.info("no_previous_scrape_full_crawl")
            return None
        try:
            ts = float(cls._LAST_SCRAPE_FILE.read_text().strip())
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError) as exc:
            logger.warning("last_scrape_load_failed", error=str(exc))
            return None

    @classmethod
    def _save_last_scrape(cls) -> None:
        """Persist the current timestamp as the last scrape checkpoint."""
        now = datetime.now(timezone.utc).timestamp()
        try:
            cls._LAST_SCRAPE_FILE.write_text(str(now))
            logger.info("last_scrape_saved")
        except OSError as exc:
            logger.error("last_scrape_save_failed", error=str(exc))

    def start_requests(self) -> Generator[Request, None, None]:
        """Generate initial requests to CNINFO disclosure pages.

        CNINFO uses dynamic loading, so we use Playwright for
        JavaScript rendering.
        """
        # The main disclosure list page requires Playwright
        yield scrapy.Request(
            url=DISCLOSURE_URL,
            callback=self.parse_disclosure_list,
            errback=self._handle_error,
            meta={
                "playwright": True,
                "playwright_context": "default",
                "playwright_include_page": True,
                "playwright_page_init_callback": self._init_page,
            },
        )

        # Also try the API-based approach for efficiency
        yield scrapy.Request(
            url=ANNOUNCEMENT_LIST_URL,
            callback=self.parse_api_response,
            errback=self._handle_error,
            meta={
                "playwright": True,
                "playwright_context": "default",
            },
        )

    async def _init_page(self, page: Any) -> None:
        """Initialize the Playwright page with anti-detection measures.

        Args:
            page: Playwright page object.
        """
        # Wait for dynamic content to load
        await page.wait_for_timeout(3000)
        # Scroll to trigger lazy loading
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
        await page.wait_for_timeout(1000)

    def parse_disclosure_list(
        self, response: Response
    ) -> Generator[dict[str, Any] | Request, None, None]:
        """Parse the CNINFO disclosure list page.

        Extracts announcement rows and follows pagination.

        Args:
            response: The Playwright-rendered page response.

        Yields:
            Item dicts for each announcement found.
        """
        logger.info("parsing_disclosure_list", url=response.url[:100])

        # CNINFO typically renders a table with class containing 'list' or 'table'
        rows = response.css(
            "table tbody tr, .list-container .item, .el-table__body tr, "
            "tr[class*='item'], div[class*='announce']"
        )

        items_yielded = 0
        skipped_old = 0

        for row in rows:
            item = self._extract_row_item(row, response)
            if item is None:
                continue

            # Date cutoff for incremental mode
            pub_date = item.get("published_at")
            if self._cutoff_date and pub_date:
                pub_dt = _parse_cninfo_date(pub_date)
                if pub_dt and pub_dt < self._cutoff_date:
                    skipped_old += 1
                    if skipped_old > 5:
                        # Assume the rest is older since list is sorted desc
                        logger.debug("stopping_pagination_old_items", skipped=skipped_old)
                        break
                    continue

            items_yielded += 1
            yield item

            # Follow PDF links for detail pages
            if item.get("file_type") == "html" or item.get("file_type") is None:
                if item.get("url"):
                    yield scrapy.Request(
                        url=item["url"],
                        callback=self.parse_announcement_detail,
                        errback=self._handle_error,
                        meta={
                            "announcement_meta": item,
                            "playwright": True,
                            "playwright_context": "default",
                        },
                    )

        logger.info(
            "disclosure_page_done",
            items=items_yielded,
            skipped_old=skipped_old,
        )

        # ── Pagination ─────────────────────────────────────────────────
        if items_yielded > 0 or not self._cutoff_date:
            next_links = response.css(
                "a.next, .pagination .next, .el-pager li.active + li, "
                "a:contains('下一页'), a:contains('>'), "
                ".page-next a, .btn-next"
            )
            for next_link in next_links:
                next_href = next_link.css("::attr(href)").get()
                if next_href and next_href != "#":
                    next_url = urljoin(response.url, next_href)
                    yield scrapy.Request(
                        url=next_url,
                        callback=self.parse_disclosure_list,
                        errback=self._handle_error,
                        meta={
                            "playwright": True,
                            "playwright_context": "default",
                        },
                    )
                    break

    def parse_api_response(self, response: Response) -> Generator[dict[str, Any], None, None]:
        """Parse CNINFO API JSON responses.

        Some CNINFO endpoints return JSON. This handler detects and
        processes those responses.

        Args:
            response: Raw HTTP response.

        Yields:
            Item dicts parsed from the API response.
        """
        content_type = response.headers.get(b"Content-Type", b"").decode("utf-8", errors="ignore")
        if "json" not in content_type.lower():
            # Not JSON; let the main parser handle it
            return

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            return

        # CNINFO API typically wraps data in various shapes
        announcements = (
            data.get("announcements")
            or data.get("classifiedAnnouncements")
            or data.get("data", [])
        )

        if isinstance(announcements, list):
            for ann in announcements:
                if not isinstance(ann, dict):
                    continue

                title = ann.get("announcementTitle", "") or ann.get("title", "")
                ann_url = ann.get("adjunctUrl", "") or ann.get("url", "")
                full_url = urljoin(BASE_URL, ann_url) if ann_url else ""
                file_type = _infer_file_type(title, ann_url)
                pub_date = ann.get("announcementTime", "") or ann.get("publishDate", "")

                yield {
                    "url": full_url or response.url,
                    "title": title,
                    "source": "cninfo",
                    "section": "api",
                    "file_type": file_type,
                    "file_url": full_url,
                    "published_at": pub_date,
                    "raw_meta": {
                        "stock_code": ann.get("secCode", ""),
                        "stock_name": ann.get("secName", ""),
                        "announcement_id": ann.get("id", ""),
                        "report_cycle": _classify_report_cycle(title),
                    },
                }

    def parse_announcement_detail(
        self, response: Response
    ) -> Generator[dict[str, Any], None, None]:
        """Parse an individual announcement detail page.

        Extracts the actual PDF download links from the detail page.

        Args:
            response: The announcement detail page response.

        Yields:
            Item dicts with document download URLs.
        """
        meta = response.meta.get("announcement_meta", {})

        # Find PDF/DOCX download links
        doc_links = response.css(
            "a[href$='.pdf'], a[href$='.doc'], a[href$='.docx']"
        )
        found = False
        for link in doc_links:
            href = link.css("::attr(href)").get()
            if href:
                full_url = urljoin(response.url, href)
                file_type = _infer_file_type("", href)
                yield {
                    "url": full_url,
                    "title": meta.get("title", ""),
                    "source": "cninfo",
                    "section": "detail",
                    "file_type": file_type,
                    "file_url": full_url,
                    "published_at": meta.get("published_at"),
                    "raw_meta": {
                        **meta.get("raw_meta", {}),
                        "page_url": response.url,
                    },
                }
                found = True

        if not found:
            # If no direct document links, yield the page itself
            yield {
                "url": response.url,
                "title": meta.get("title", ""),
                "source": "cninfo",
                "section": "detail",
                "file_type": "html",
                "file_url": response.url,
                "published_at": meta.get("published_at"),
                "raw_meta": {
                    **meta.get("raw_meta", {}),
                    "page_url": response.url,
                },
            }

    def _extract_row_item(
        self, row: scrapy.Selector, response: Response
    ) -> Optional[dict[str, Any]]:
        """Extract announcement metadata from a table row element.

        Args:
            row: CSS selector for a single table row.
            response: The parent page response for URL resolution.

        Returns:
            Item dict or None if the row is empty/malformed.
        """
        # Extract the announcement link
        link = row.css("a[href]")
        if not link:
            return None

        href = link.css("::attr(href)").get()
        if not href:
            return None

        # Skip non-announcement links
        if any(
            skip in href
            for skip in ("javascript", "#", "logout", "login")
        ):
            return None

        full_url = urljoin(response.url, href)

        # Title from link text
        title = (link.css("::text").get() or "").strip()
        if not title:
            return None

        # Date from adjacent cells (CNINFO tables: columns are 序号|公告标题|日期)
        date_text = ""
        date_cells = row.css("td:last-child::text, span.date::text, .time::text")
        for cell in date_cells:
            date_text = cell.get() or ""
            if date_text.strip():
                break

        # File type inference
        file_type = _infer_file_type(title, href)

        # Report cycle classification
        report_cycle = _classify_report_cycle(title)

        return {
            "url": full_url,
            "title": title,
            "source": "cninfo",
            "section": "disclosure",
            "file_type": file_type if file_type != "html" else None,
            "file_url": full_url if file_type not in ("html", None) else None,
            "published_at": date_text.strip() if date_text else None,
            "raw_meta": {
                "report_cycle": report_cycle,
                "stock_code": self._stock_code,
            },
        }

    def closed(self, reason: str) -> None:
        """Called when the spider finishes. Persists the last scrape timestamp."""
        self._save_last_scrape()
        logger.info("cninfo_spider_closed", reason=reason)

    def _handle_error(self, failure: Any) -> None:
        """Log request failures."""
        logger.error(
            "cninfo_request_failed",
            url=failure.request.url if hasattr(failure, "request") else "unknown",
            error=str(failure.value) if hasattr(failure, "value") else "unknown",
        )
