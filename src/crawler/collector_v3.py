"""Enhanced Report Collector v3 — Playwright + recursive + DB + ES.

Extends collector_v2 with:
  1. Playwright fallback for JS-rendered pages
  2. Recursive sub-page crawling for PDF discovery
  3. Circuit breaker for error-prone sites
  4. File download with SHA-256 to /tmp/environmental_downloads/
  5. Async PostgreSQL save via SQLAlchemy
  6. Elasticsearch indexing
  7. --mode flag: daily / full / test
  8. Per-province progress output
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# ═══ Import from v2 ═══
# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from src.crawler.collector_v2 import (
    CHINESE_DATE,
    DATA_DIR,
    DOC_PATTERN,
    DOWNLOAD_DIR,
    REPORT_KEYS,
    USER_AGENT,
    Fetcher,
    ReportCollectorV2,
    extract_links,
    fetcher as v2_fetcher,
)

# ═══ Async DB + ES imports ═══
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from elasticsearch import AsyncElasticsearch

# ═══ Playwright ═══
from playwright.async_api import async_playwright

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/environmental",
)
ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")

# Paths to try per bureau (same as v2)
BUREAU_PATHS = ["/xxgk/", "/zwgk/", "/gsgg/", "/tzgg/"]

# Circuit breaker: skip site after N consecutive errors
CIRCUIT_BREAKER_THRESHOLD = 3

# Sub-page crawling: max pages to recurse into per bureau
MAX_SUB_PAGES = 5

# ═══════════════════════════════════════════════════════════════════════════════
# Playwright fetcher
# ═══════════════════════════════════════════════════════════════════════════════


class PlaywrightFetcher:
    """Fetches page content using Playwright (JS rendering)."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    async def _ensure_browser(self):
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

    async def fetch(self, url: str, timeout: int = 30000) -> tuple[str, str]:
        """Fetch a page with JS rendering. Returns (html, effective_url)."""
        await self._ensure_browser()
        context = None
        page = None
        try:
            context = await self._browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=timeout)
            html = await page.content()
            effective_url = page.url
            return html, effective_url
        except Exception as e:
            return "", str(e)
        finally:
            if page:
                await page.close()
            if context:
                await context.close()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()


pw_fetcher = PlaywrightFetcher()


# ═══════════════════════════════════════════════════════════════════════════════
# Sub-page recursive crawling
# ═══════════════════════════════════════════════════════════════════════════════


def _looks_like_list_page(soup) -> bool:
    """Heuristic: does this page look like a list/news listing?"""
    from bs4 import BeautifulSoup

    # Has multiple links with dates or report keywords
    links = soup.find_all("a", href=True)
    report_links = [a for a in links if REPORT_KEYS.search(a.get_text(strip=True))]
    if len(report_links) >= 3:
        return True
    # Has typical list structure
    lists = soup.find_all(["ul", "ol"])
    list_links = sum(1 for l in lists if len(l.find_all("a", href=True)) >= 3)
    if list_links >= 1:
        return True
    return False


def _extract_sub_page_links(html: str, base_url: str) -> list[str]:
    """Extract internal links that might contain PDF downloads.

    Only returns links:
    - Same domain as base_url
    - Not a direct document (PDF/DOCX etc.) — those are already caught
    - Has Chinese text indicating a report/news page
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    base_domain = urlparse(base_url).netloc
    candidates = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Must be same domain
        if parsed.netloc and parsed.netloc != base_domain:
            continue
        # Skip anchors, javascript, same-page
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        # Skip direct document downloads (already caught by extract_links)
        if DOC_PATTERN.search(href):
            continue
        # Skip static resources
        if any(ext in parsed.path for ext in [".css", ".js", ".png", ".jpg", ".gif", ".ico"]):
            continue

        text = a.get_text(strip=True)
        # Must have report-related keywords or look like a news link
        if REPORT_KEYS.search(text) or REPORT_KEYS.search(full_url):
            candidates.append(full_url)
        elif len(text) >= 4 and ("/" in parsed.path.strip("/").split("/")[-1] or
                                 any(k in text for k in ["通知", "公告", "公示"])):
            candidates.append(full_url)

    # Deduplicate and limit
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique[:MAX_SUB_PAGES]


# ═══════════════════════════════════════════════════════════════════════════════
# DB + ES helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def _ensure_reports_table():
    """Create the reports table if it doesn't exist."""
    from src.models.database import Base
    from src.models.report import Report

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


def _make_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _extract_enterprise(title: str) -> str:
    """Extract enterprise name from report title."""
    import re
    # Common Chinese enterprise patterns: XXX有限公司, XXX公司, XXX集团, XXX厂
    m = re.search(
        r'([\u4e00-\u9fff（）()]{2,60}(?:有限公司|有限责任公司|股份有限公司|集团公司|集团|公司|厂|研究院|研究所))',
        title
    )
    if m:
        return m.group(1)
    return ""


async def save_to_postgres(reports: list[dict]) -> int:
    """Save collected reports to PostgreSQL 'reports' table.

    Each report dict should have: url, title, province, file_type
    """
    from src.models.database import Base
    from src.models.report import Report

    await _ensure_reports_table()

    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    saved = 0
    async with session_factory() as session:
        for r in reports:
            url_hash = _make_url_hash(r["url"])
            # Check existing
            existing = await session.execute(
                select(Report).where(Report.url_hash == url_hash)
            )
            if existing.scalar_one_or_none():
                continue

            report = Report(
                url_hash=url_hash,
                url=r.get("url", ""),
                title=r.get("title"),
                source=r.get("province", r.get("source", "unknown")),
                section=r.get("section", "bureau"),
                file_type=r.get("file_type", "html"),
                file_url=r.get("url", ""),
                published_at=datetime.now(timezone.utc) if r.get("published_at") else None,
                scraped_at=datetime.now(timezone.utc),
                raw_meta={
                    "bureau_url": r.get("source_url"),
                    "province": r.get("province", ""),
                },
            )
            session.add(report)
            saved += 1

        await session.commit()

    await engine.dispose()
    return saved


async def index_to_elasticsearch(reports: list[dict]) -> int:
    """Index reports to Elasticsearch 'reports' index.

    Each report dict should have: url, title, province, file_type
    """
    es = AsyncElasticsearch(ELASTICSEARCH_URL)

    # Ensure index exists with mapping
    exists = await es.indices.exists(index="reports")
    if not exists:
        await es.indices.create(
            index="reports",
            body={
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {
                    "properties": {
                        "url": {"type": "keyword"},
                        "title": {"type": "text", "analyzer": "standard"},
                        "province": {"type": "keyword"},
                        "file_type": {"type": "keyword"},
                        "url_hash": {"type": "keyword"},
                        "scraped_at": {"type": "date"},
                        "content_text": {"type": "text", "analyzer": "standard"},
                    }
                },
            },
        )

    indexed = 0
    for r in reports:
        try:
            doc = {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("title", ""),
                "province": r.get("province", r.get("source", "unknown")),
                "city": r.get("city", ""),
                "enterprise": r.get("enterprise", ""),
                "file_type": r.get("file_type", "html"),
                "file_hash": "",
                "ocr_status": "pending",
                "url_hash": _make_url_hash(r["url"]),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            await es.index(index="reports", id=doc["url_hash"], body=doc, refresh="wait_for")
            indexed += 1
        except Exception as e:
            print(f"    ⚠️ ES index error: {e}")

    await es.close()
    return indexed


async def download_and_hash(url: str, download_dir: Path) -> Optional[Path]:
    """Download a file, save with SHA-256 hash prefix, return path."""
    content = await v2_fetcher.fetch_file(url)
    if not content or len(content) < 128:
        return None

    full_hash = hashlib.sha256(content).hexdigest()
    ext = DOC_PATTERN.search(url)
    ext = ext.group(1).lower() if ext else "bin"
    filename = f"{full_hash}.{ext}"
    filepath = download_dir / filename

    if not filepath.exists():
        filepath.write_bytes(content)

    # Save SHA-256 manifest entry
    manifest_path = download_dir / "sha256_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text("utf-8"))
    else:
        manifest = {}
    manifest[full_hash] = {
        "url": url,
        "file": filename,
        "sha256": full_hash,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return filepath


# ═══════════════════════════════════════════════════════════════════════════════
# ReportCollectorV3 — extends V2
# ═══════════════════════════════════════════════════════════════════════════════


class ReportCollectorV3(ReportCollectorV2):
    """Enhanced report collector with Playwright, recursion, circuit breaker, DB + ES."""

    def __init__(self, mode: str = "test"):
        super().__init__()
        self.mode = mode
        self.circuit_breaker: dict[str, int] = {}  # domain -> consecutive errors
        self.all_reports: list[dict] = []
        self.bureau_errors: list[str] = []

    async def fetch_with_fallback(self, url: str) -> tuple[str, str]:
        """Try httpx first, fallback to Playwright if content is empty/small.

        Returns (html_text, effective_url).
        """
        html, effective = await v2_fetcher.fetch(url)
        if html and len(html) > 200:
            return html, effective

        # Fallback to Playwright
        print(f"    🔄 JS fallback for {url[:80]}...")
        try:
            pw_html, pw_effective = await pw_fetcher.fetch(url)
            if pw_html and len(pw_html) > 200:
                return pw_html, pw_effective
        except Exception as e:
            print(f"    ⚠️ Playwright also failed: {e}")

        return "", ""

    async def collect_bureau(self, name: str, url: str, province: str) -> list[dict]:
        """Collect reports from one bureau with circuit breaker + recursion.

        Returns list of report dicts.
        """
        domain = urlparse(url).netloc

        # Circuit breaker check
        if self.circuit_breaker.get(domain, 0) >= CIRCUIT_BREAKER_THRESHOLD:
            print(f"  ⛔ Circuit breaker: skipping {domain} ({CIRCUIT_BREAKER_THRESHOLD}+ errors)")
            self.bureau_errors.append(f"{province}: circuit breaker tripped")
            return []

        reports = []
        domain_errors = 0

        for path in BUREAU_PATHS:
            target = urljoin(url, path)
            try:
                html, effective_url = await self.fetch_with_fallback(target)
                if not html or len(html) < 100:
                    domain_errors += 1
                    continue

                self.stats["pages"] += 1
                # Reset errors on success
                domain_errors = 0

                # Extract links from this page
                links = extract_links(html, effective_url)
                for link in links:
                    link["province"] = province
                    link["source"] = province
                    link["source_url"] = url
                    link["enterprise"] = _extract_enterprise(link.get("title", ""))
                    reports.append(link)

                # Recursive sub-page crawling: if we found report links (HTML pages),
                # dive into them to find PDF attachments
                if links:
                    sub_links = _extract_sub_page_links(html, effective_url)
                    for sub_url in sub_links:
                        try:
                            sub_html, sub_effective = await self.fetch_with_fallback(sub_url)
                            if sub_html and len(sub_html) > 200:
                                self.stats["pages"] += 1
                                sub_links_found = extract_links(sub_html, sub_effective)
                                for sl in sub_links_found:
                                    sl["province"] = province
                                    sl["source"] = province
                                    sl["source_url"] = url
                                    reports.append(sl)
                                if sub_links_found:
                                    print(f"      📄 sub-page → {len(sub_links_found)} docs")
                            await asyncio.sleep(0.3)
                        except Exception:
                            pass

                    print(f"  ✅ {path} → {len(links)} reports (+ sub-pages)")
                else:
                    print(f"  ·  {path} — 0 reports")

            except Exception as e:
                domain_errors += 1
                print(f"  ❌ {path}: {e}")

            await asyncio.sleep(0.5)

        # Update circuit breaker
        if domain_errors > 0:
            self.circuit_breaker[domain] = self.circuit_breaker.get(domain, 0) + domain_errors
        else:
            self.circuit_breaker[domain] = 0

        self.stats["reports"] += len(reports)
        self.all_reports.extend(reports)
        return reports

    async def download_report(self, url: str) -> Optional[Path]:
        """Download a report file with SHA-256 hashing to /tmp/environmental_downloads/."""
        return await download_and_hash(url, self.download_dir)

    async def run_full(self):
        """Run collection across all provinces, save to PG + ES, download files.

        Mode behavior:
        - test: 1 province (first in list)
        - daily: all provinces, no downloads (just metadata collection)
        - full: all provinces + downloads
        """
        bureaus = json.loads((DATA_DIR / "env_bureaus.json").read_text("utf-8"))
        provinces = bureaus.get("provinces", {})

        if self.mode == "test":
            # Test mode: use specific test province (Guangdong has best coverage)
            test_province = os.getenv("TEST_PROVINCE", "广东")
            test_items = [(k, v) for k, v in provinces.items() if k == test_province]
            prov_items = test_items if test_items else list(provinces.items())[:1]
        else:
            prov_items = list(provinces.items())

        print("=" * 60)
        print(f"🌍 Environmental Report Crawler v3 [mode={self.mode}]")
        print(f"   Provinces to crawl: {len(prov_items)}")
        print("=" * 60)

        all_reports = []
        total_provinces = len(prov_items)

        for i, (prov, info) in enumerate(prov_items, 1):
            if not isinstance(info, dict):
                continue
            name = info.get("name", prov)
            url = info.get("url", "")
            print(f"\n[{i}/{total_provinces}] 📍 {prov}: {name}")
            print(f"   URL: {url}")

            start_t = time.time()
            try:
                reports = await self.collect_bureau(name, url, prov)
                elapsed = time.time() - start_t
                print(f"   ⏱ {elapsed:.1f}s | {len(reports)} reports found")
                all_reports.extend(reports)
            except Exception as e:
                elapsed = time.time() - start_t
                print(f"   ❌ Failed after {elapsed:.1f}s: {e}")
                self.bureau_errors.append(f"{prov}: {e}")

        print("\n" + "=" * 60)
        print(f"📊 Collection Complete: {len(all_reports)} total reports from {len(prov_items)} provinces")
        if self.bureau_errors:
            print(f"⚠️  Errors: {len(self.bureau_errors)} bureaus had issues")
            for e in self.bureau_errors[:5]:
                print(f"   - {e}")

        # Phase 2: Save to PostgreSQL
        if all_reports:
            print(f"\n💾 Saving {len(all_reports)} reports to PostgreSQL...")
            try:
                saved = await save_to_postgres(all_reports)
                print(f"   ✅ {saved} new records inserted to PostgreSQL 'reports' table")
            except Exception as e:
                print(f"   ❌ PG save failed: {e}")

            # Phase 3: Index to Elasticsearch
            print(f"\n🔍 Indexing {len(all_reports)} reports to Elasticsearch...")
            try:
                indexed = await index_to_elasticsearch(all_reports)
                print(f"   ✅ {indexed} documents indexed to ES 'reports' index")
            except Exception as e:
                print(f"   ❌ ES indexing failed: {e}")

        # Phase 4: Download files (only in full mode)
        if self.mode == "full" and all_reports:
            print(f"\n📥 Downloading report files to {DOWNLOAD_DIR}...")
            pdf_reports = [r for r in all_reports if r.get("file_type") == "pdf"]
            for r in pdf_reports[:20]:  # Limit to 20 for now
                filepath = await self.download_report(r["url"])
                if filepath:
                    print(f"   ✅ {filepath.name}")
                await asyncio.sleep(0.5)

        self.results = all_reports
        self._save_v3()

    def _save_v3(self):
        """Save run results with extended metadata."""
        manifest = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "stats": self.stats,
            "bureau_errors": self.bureau_errors,
            "circuit_breaker": self.circuit_breaker,
            "total_reports": len(self.all_reports),
            "results": self.results[:1000],  # Truncate for file size
        }
        (self.download_dir / "run_results_v3.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n📄 Saved: {self.download_dir}/run_results_v3.json")
        print(f"📊 Pages: {self.stats['pages']} | Reports: {self.stats['reports']} | Downloaded: {self.stats['downloaded']}")

    async def cleanup(self):
        """Close all async resources."""
        await v2_fetcher.close()
        await pw_fetcher.close()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Environmental Report Collector v3")
    parser.add_argument(
        "--mode",
        choices=["daily", "full", "test"],
        default="test",
        help="Crawl mode: test (1 province), daily (all, metadata only), full (all + downloads)",
    )
    args = parser.parse_args()

    async def _run():
        collector = ReportCollectorV3(mode=args.mode)
        try:
            await collector.run_full()
        finally:
            await collector.cleanup()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
