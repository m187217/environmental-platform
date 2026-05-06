"""Environmental Report Collector v2 — 真实 HTTP 爬取版

直接采集:
  1. 环境局官网 → 解析公告列表 → 提取环评报告 PDF 链接 → 下载
  2. 化工园区 → 解析企业名录 → 提取企业全名
  3. 联网搜索 → 按企业名搜索环评报告
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

# ═══ Constants ═══

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/environmental_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

DOC_PATTERN = re.compile(r'\.(pdf|docx?|xlsx?|pptx?)$', re.I)
REPORT_KEYS = re.compile(
    r'(环评|环境影响|环境评估|竣工环保|验收监测|排污许可|应急预案|清洁生产|环保验收|报告书|报告表|EIA)',
    re.I,
)
CHINESE_DATE = re.compile(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?')
HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)

# ═══ HTTP Client ═══


class Fetcher:
    """Async HTTP fetcher with retry and rate limiting."""

    def __init__(self):
        self._client = None
        self._sem = asyncio.Semaphore(3)  # Max concurrent

    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=httpx.Timeout(30),
                follow_redirects=True,
                verify=False,
            )

    async def fetch(self, url: str) -> tuple[str, str]:
        """Fetch URL, return (text, effective_url)."""
        await self._ensure_client()
        async with self._sem:
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.text, str(resp.url)
            except Exception as e:
                return "", str(e)

    async def fetch_file(self, url: str) -> Optional[bytes]:
        """Fetch binary file."""
        await self._ensure_client()
        async with self._sem:
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.content
            except Exception:
                return None

    async def close(self):
        if self._client:
            await self._client.aclose()


fetcher = Fetcher()


def extract_links(html: str, base_url: str) -> list[dict]:
    """Extract document links with metadata from HTML."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    results = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        title = a.get_text(strip=True) or a.get("title", "")
        parent_text = a.parent.get_text(strip=True)[:200] if a.parent else ""

        # Only collect document links or report-related links
        is_doc = bool(DOC_PATTERN.search(href))
        is_report = bool(REPORT_KEYS.search(title + parent_text))

        if is_doc or is_report:
            file_type = DOC_PATTERN.search(href).group(1).lower() if is_doc else "html"
            date_match = CHINESE_DATE.search(title + parent_text)
            results.append({
                "url": full_url,
                "title": title,
                "file_type": file_type,
                "published_at": date_match.group(0) if date_match else None,
            })

    return results


# ═══ Collector ═══


class ReportCollectorV2:
    """Real HTTP-based report collector."""

    def __init__(self):
        self.download_dir = DOWNLOAD_DIR
        self.results: list[dict] = []
        self.stats = {"pages": 0, "reports": 0, "downloaded": 0, "failed": 0}

    async def collect_bureau(self, name: str, url: str, province: str) -> list[dict]:
        """Collect reports from one environmental bureau website."""
        reports = []
        paths = ["/xxgk/", "/zwgk/", "/gsgg/", "/tzgg/"]

        for path in paths:
            target = urljoin(url, path)
            try:
                html, _ = await fetcher.fetch(target)
                if not html or len(html) < 100:
                    continue
                self.stats["pages"] += 1

                links = extract_links(html, target)
                for link in links:
                    link["source"] = province
                    link["source_url"] = url
                    reports.append(link)

                if links:
                    print(f"  ✅ {path} → {len(links)} reports")
                else:
                    print(f"  ·  {path} — 0 reports")

            except Exception as e:
                print(f"  ❌ {path}: {e}")

            await asyncio.sleep(0.5)

        self.stats["reports"] += len(reports)
        return reports

    async def collect_park_enterprises(self, park: dict) -> list[str]:
        """Try to extract enterprise names from a park website."""
        name = park["name"]
        url = park["url"]
        enterprises = []
        paths = ["/qyml/", "/gsgg/", "/tzgg/"]

        for path in paths:
            target = urljoin(url, path)
            try:
                html, _ = await fetcher.fetch(target)
                if not html or len(html) < 100:
                    continue
                self.stats["pages"] += 1

                # Look for enterprise name patterns in HTML
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                for tag in soup.find_all(["a", "li", "td", "span"]):
                    text = tag.get_text(strip=True)
                    if _is_enterprise_name(text):
                        enterprises.append(text)

                if enterprises:
                    # Deduplicate
                    enterprises = list(set(enterprises))
                    print(f"  ✅ {path} → {len(enterprises)} enterprises")

            except Exception:
                pass

            await asyncio.sleep(0.3)

        return enterprises

    async def download_report(self, url: str) -> Optional[Path]:
        """Download a report file."""
        content = await fetcher.fetch_file(url)
        if not content or len(content) < 1024:
            return None

        ext = DOC_PATTERN.search(url)
        ext = ext.group(1).lower() if ext else "bin"
        h = hashlib.sha256(content).hexdigest()[:12]
        filename = f"{h}.{ext}"

        filepath = self.download_dir / filename
        if not filepath.exists():
            filepath.write_bytes(content)
            self.stats["downloaded"] += 1
            return filepath
        return None

    async def run_full(self):
        """Run full collection across all sources."""
        bureaus = json.loads((DATA_DIR / "env_bureaus.json").read_text("utf-8"))
        parks = json.loads((DATA_DIR / "chemical_parks.json").read_text("utf-8"))

        print("=" * 60)
        print("🌍 Phase 1: Environmental Bureau Reports")
        print("=" * 60)

        all_reports = []
        provinces = bureaus.get("provinces", {})
        for i, (prov, info) in enumerate(provinces.items()):
            if not isinstance(info, dict):
                continue
            print(f"\n[{i+1}/{len(provinces)}] {prov}: {info['url'][:60]}")
            reports = await self.collect_bureau(info["name"], info["url"], prov)
            all_reports.extend(reports)

        print("\n" + "=" * 60)
        print("🏭 Phase 2: Chemical Park Enterprises")
        print("=" * 60)

        for i, park in enumerate(parks):
            print(f"\n[{i+1}/{len(parks)}] {park['name']}")
            enterprises = await self.collect_park_enterprises(park)
            park["enterprises"] = enterprises
            if enterprises:
                # Search reports for each enterprise
                for ent in enterprises[:3]:  # Limit per park
                    search_url = f"https://www.google.com/search?q={ent}+环境评估报告+filetype:pdf"
                    self.results.append({
                        "enterprise": ent,
                        "park": park["name"],
                        "search_url": search_url,
                    })

        print("\n" + "=" * 60)
        print("📥 Phase 3: Download Sample Reports")
        print("=" * 60)

        # Download top 10 report files
        pdf_reports = [r for r in all_reports if r.get("file_type") == "pdf"]
        for r in pdf_reports[:10]:
            filepath = await self.download_report(r["url"])
            if filepath:
                print(f"  ✅ {filepath.name}")
            await asyncio.sleep(0.5)

        # Save results
        self._save()

    def _save(self):
        manifest = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "stats": self.stats,
            "results": self.results,
        }
        (self.download_dir / "run_results.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n📄 Saved: {self.download_dir}/run_results.json")
        print(f"📊 Pages: {self.stats['pages']} | Reports: {self.stats['reports']} | Downloaded: {self.stats['downloaded']}")


def _is_enterprise_name(text: str) -> bool:
    """Heuristic: does text look like a Chinese enterprise name?"""
    if len(text) < 4 or len(text) > 60:
        return False
    if not re.search(r'[\u4e00-\u9fff]', text):
        return False
    # Must contain company type keywords
    company_keys = ['公司', '集团', '厂', '企业', '有限', '股份']
    return any(k in text for k in company_keys)
