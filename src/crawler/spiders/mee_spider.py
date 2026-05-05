"""Spider for 生态环境部 (Ministry of Ecology and Environment).

Target: https://www.mee.gov.cn/

Crawls government environmental reports, policy documents, and public
disclosures. Finds PDF, DOCX, and other document links. Handles pagination
across multiple sections of the site.

Sections crawled:
  - 新闻发布 (Press releases)
  - 政策文件 (Policy documents)
  - 环境质量 (Environmental quality reports)
  - 业务工作 (Operational work)
  - 信息公开 (Information disclosure)

Usage:
    scrapy crawl mee          # Full crawl
    scrapy crawl mee -a days=7  # Last 7 days only (incremental)
    scrapy crawl mee -a section=ywgz  # Single section
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Optional
from urllib.parse import urljoin, urlparse

import scrapy
import structlog
from scrapy.http import Request, Response

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "https://www.mee.gov.cn"

# Sections with their entry-point URLs and display names
SECTIONS: dict[str, dict[str, str]] = {
    "xwfb": {
        "name": "新闻发布",
        "url": "https://www.mee.gov.cn/ywdt/xwfb/",
    },
    "zcwj": {
        "name": "政策文件",
        "url": "https://www.mee.gov.cn/ywgz/zcwj/",
    },
    "hjzl": {
        "name": "环境质量",
        "url": "https://www.mee.gov.cn/hjzl/",
    },
    "ywgz": {
        "name": "业务工作",
        "url": "https://www.mee.gov.cn/ywgz/",
    },
    "xxgk": {
        "name": "信息公开",
        "url": "https://www.mee.gov.cn/xxgk2018/",
    },
}

# Document file extensions we're interested in
DOCUMENT_EXTENSIONS: set[str] = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

# Regex to extract Chinese dates from text like "2024-05-06" or "2024年05月06日"
DATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?"),
    re.compile(r"(\d{4})(\d{2})(\d{2})"),  # 20240506 compact form
]

# Pagination CSS selectors — varies by section, so we try multiple
PAGINATION_SELECTORS: list[str] = [
    "a.next",                     # class="next"
    "a:contains('下一页')",        # text: 下一页
    "a:contains('>')",            # text: >
    ".pagination a:last-child",
    "a[href*='index_']",          # MEE uses index_1, index_2, etc.
]

# Report link selectors — typical MEE list page structure
LIST_SELECTORS: list[str] = [
    "ul.list li a",               # Common MEE list pattern
    "div.news_list ul li a",
    ".list_content a[href]",
    ".main-content a[href]",
    "a[href*='.pdf'], a[href*='.doc']",
    "a[href*='/xxgk']",
    "a[href*='/content']",
]


def _extract_date_from_text(text: str) -> Optional[datetime]:
    """Attempt to parse a Chinese-format date from a string.

    Args:
        text: Raw text potentially containing a date.

    Returns:
        A timezone-aware UTC datetime, or None if parsing fails.
    """
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                year = int(match.group(1))
                month = int(match.group(2))
                day = int(match.group(3))
                return datetime(year, month, day, tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue
    return None


def _is_document_url(url: str) -> bool:
    """Check if a URL points to a document file based on extension."""
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    return any(path_lower.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


def _classify_file_type(url: str) -> Optional[str]:
    """Get the file type from a URL extension.

    Args:
        url: The URL to inspect.

    Returns:
        Lowercase extension without dot (e.g. 'pdf'), or None.
    """
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    for ext in DOCUMENT_EXTENSIONS:
        if path_lower.endswith(ext):
            return ext.lstrip(".")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Spider
# ═══════════════════════════════════════════════════════════════════════════════


class MeeSpider(scrapy.Spider):
    """Scrapes environmental reports from 生态环境部."""

    name = "mee"
    allowed_domains = ["www.mee.gov.cn", "mee.gov.cn"]
    start_urls = [section["url"] for section in SECTIONS.values()]

    custom_settings: dict[str, Any] = {
        "DOWNLOAD_DELAY": 0.5,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
    }

    def __init__(
        self,
        days: Optional[str] = None,
        section: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Initialize the MEE spider.

        Args:
            days: If set, only crawl items from the last N days (incremental mode).
            section: If set, only crawl a single section key (e.g. 'ywgz').
        """
        super().__init__(*args, **kwargs)
        self._cutoff_date: Optional[datetime] = None
        if days is not None:
            try:
                days_int = int(days)
                self._cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_int)
                logger.info("incremental_mode", cutoff=str(self._cutoff_date))
            except ValueError:
                logger.warning("invalid_days_param", value=days)

        self._target_section: Optional[str] = section
        if section and section in SECTIONS:
            self.start_urls = [SECTIONS[section]["url"]]
            logger.info("single_section_mode", section=section)
        elif section:
            logger.warning("unknown_section", section=section, known=list(SECTIONS.keys()))

    def start_requests(self) -> Generator[Request, None, None]:
        """Generate initial requests, optionally using Playwright for JS-heavy pages."""
        for url in self.start_urls:
            yield scrapy.Request(
                url=url,
                callback=self.parse_list,
                errback=self._handle_error,
                meta={
                    "playwright": True,
                    "playwright_context": "default",
                    "playwright_include_page": True,
                },
            )

    def parse_list(self, response: Response) -> Generator[Request, None, None]:
        """Parse a list page, extracting report links and pagination.

        Args:
            response: The HTTP response for a section list page.

        Yields:
            Requests for individual report pages and subsequent list pages.
        """
        section_key = self._get_section_from_url(response.url)
        logger.debug(
            "parsing_list",
            url=response.url,
            section=section_key,
            status=response.status,
        )

        # ── Extract report links ───────────────────────────────────────────
        links_found = 0
        skipped_by_date = 0

        for selector in LIST_SELECTORS:
            for link in response.css(selector):
                href = link.css("::attr(href)").get()
                if not href:
                    continue

                full_url = urljoin(response.url, href)

                # Extract title from link text or parent tree
                title = (
                    link.css("::text").get()
                    or link.css("a::attr(title)").get()
                    or ""
                ).strip()

                # Try to extract date from surrounding text
                parent_text = link.xpath("..//text()").get() or ""
                pub_date = _extract_date_from_text(title + parent_text)

                # Date cutoff for incremental mode
                if self._cutoff_date and pub_date and pub_date < self._cutoff_date:
                    skipped_by_date += 1
                    continue

                # If it's a direct document link, yield immediately
                if _is_document_url(full_url):
                    file_type = _classify_file_type(full_url)
                    yield {
                        "url": full_url,
                        "title": title or "Untitled Document",
                        "source": "mee",
                        "section": section_key,
                        "file_type": file_type,
                        "file_url": full_url,
                        "published_at": pub_date.isoformat() if pub_date else None,
                        "raw_meta": {
                            "page_url": response.url,
                            "section": section_key,
                        },
                    }
                    links_found += 1
                else:
                    # Follow to detail page to find document links
                    yield scrapy.Request(
                        url=full_url,
                        callback=self.parse_detail,
                        errback=self._handle_error,
                        meta={
                            "title": title,
                            "section": section_key,
                            "published_at": pub_date,
                            "playwright": True,
                            "playwright_context": "default",
                        },
                    )
                    links_found += 1

        logger.info(
            "list_page_parsed",
            url=response.url,
            section=section_key,
            links_found=links_found,
            skipped_by_date=skipped_by_date,
        )

        # ── Handle pagination ──────────────────────────────────────────────
        if links_found > 0 or not self._cutoff_date:
            for selector in PAGINATION_SELECTORS:
                next_link = response.css(selector)
                if next_link:
                    next_href = next_link.css("::attr(href)").get()
                    if next_href and next_href != "#":
                        next_url = urljoin(response.url, next_href)
                        if next_url != response.url:
                            yield scrapy.Request(
                                url=next_url,
                                callback=self.parse_list,
                                errback=self._handle_error,
                                meta={
                                    "playwright": True,
                                    "playwright_context": "default",
                                },
                            )
                            break  # Only follow one pagination match

    def parse_detail(self, response: Response) -> Generator[dict[str, Any], None, None]:
        """Parse a detail page, extracting embedded document links.

        Args:
            response: The HTTP response for a report detail page.

        Yields:
            Item dicts containing report metadata and document URLs.
        """
        title = response.meta.get("title", "")
        section = response.meta.get("section", "unknown")
        pub_date = response.meta.get("published_at")

        # Try to extract title from page if not known
        if not title:
            title = (
                response.css("h1::text").get()
                or response.css(".article-title::text").get()
                or response.css("title::text").get()
                or "Untitled"
            ).strip()

        # Try date from page metadata if not known
        if not pub_date:
            date_text = (
                response.css(".article-date::text").get()
                or response.css(".info span::text").get()
                or ""
            )
            pub_date = _extract_date_from_text(date_text)

        # Find all document links on the page
        doc_links: list[str] = []

        # Direct document links
        for doc_link in response.css(
            "a[href$='.pdf'], a[href$='.doc'], a[href$='.docx'], "
            "a[href$='.xls'], a[href$='.xlsx'], a[href$='.ppt'], a[href$='.pptx']"
        ):
            href = doc_link.css("::attr(href)").get()
            if href:
                doc_links.append(urljoin(response.url, href))

        # Links whose text or title hints at documents
        for doc_link in response.css("a"):
            link_text = (doc_link.css("::text").get() or "").lower()
            link_title = (doc_link.css("::attr(title)").get() or "").lower()
            if any(
                kw in link_text + link_title
                for kw in ("pdf", "下载", "附件", "全文", "download", "doc")
            ):
                href = doc_link.css("::attr(href)").get()
                if href and href not in doc_links:
                    full = urljoin(response.url, href)
                    if _is_document_url(full):
                        doc_links.append(full)

        if not doc_links:
            # No document links found; still record the page as a reference
            yield {
                "url": response.url,
                "title": title,
                "source": "mee",
                "section": section,
                "file_type": "html",
                "file_url": response.url,
                "published_at": pub_date.isoformat() if pub_date else None,
                "raw_meta": {"page_url": response.url, "section": section},
            }
        else:
            for doc_url in doc_links:
                file_type = _classify_file_type(doc_url)
                yield {
                    "url": doc_url,
                    "title": title,
                    "source": "mee",
                    "section": section,
                    "file_type": file_type,
                    "file_url": doc_url,
                    "published_at": pub_date.isoformat() if pub_date else None,
                    "raw_meta": {
                        "page_url": response.url,
                        "section": section,
                    },
                }

    def _handle_error(self, failure: Any) -> None:
        """Log request failures gracefully."""
        logger.error(
            "request_failed",
            url=failure.request.url if hasattr(failure, "request") else "unknown",
            error=str(failure.value) if hasattr(failure, "value") else "unknown",
        )

    @staticmethod
    def _get_section_from_url(url: str) -> str:
        """Map a URL back to a section key."""
        for key, info in SECTIONS.items():
            if info["url"] in url:
                return key
        return "unknown"
