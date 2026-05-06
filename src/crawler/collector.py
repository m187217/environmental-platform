"""Environmental Report Collector — 环境评估报告自动采集系统

Multi-source data collection pipeline:
  1. 环境局官网 → 爬取公示的环评报告文件 (PDF/DOCX)
  2. 化工园区 → 采集入园企业名单
  3. 全网搜索 → 按企业名查找环评报告
  4. 文件下载 → 存储到 MinIO 或本地目录

Usage:
    python -m src.crawler.collector bureaus          # 从环境局采集
    python -m src.crawler.collector parks             # 从化工园区采集
    python -m src.crawler.collector search <name>     # 搜索企业环评
    python -m src.crawler.collector full              # 全量采集
"""

from __future__ import annotations

import argparse
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
from urllib.parse import urljoin, urlparse

# ═══ Constants ═════════════════════════════════════════════════════════════════

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/environmental_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

ENV_BUREAUS_FILE = DATA_DIR / "env_bureaus.json"
CHEMICAL_PARKS_FILE = DATA_DIR / "chemical_parks.json"

DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
CHINESE_DATE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?")
REPORT_KEYWORDS = [
    "环评", "环境评估", "环境影响", "环境报告", "竣工环保", "验收监测",
    "排污许可", "应急预案", "清洁生产", "环保验收", "EIA", "环境影响评价",
    "建设项目环境影响", "报告书", "报告表", "登记表",
]

# ═══ Helpers ════════════════════════════════════════════════════════════════════


def load_json(path: Path) -> dict | list:
    """Load a JSON data file."""
    if not path.exists():
        print(f"⚠️  Data file not found: {path}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def classify_url(url: str) -> Optional[str]:
    """Determine file type from URL extension."""
    ext = Path(urlparse(url).path).suffix.lower()
    return ext.lstrip(".") if ext in DOC_EXTENSIONS else None


def extract_date(text: str) -> Optional[str]:
    """Extract Chinese date from text."""
    m = CHINESE_DATE.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return None


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def is_env_report(title: str) -> bool:
    """Check if title suggests an environmental assessment report."""
    return any(kw in title for kw in REPORT_KEYWORDS)


# ═══ Collector ═════════════════════════════════════════════════════════════════


class ReportCollector:
    """Orchestrates multi-source environmental report collection."""

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or DOWNLOAD_DIR
        self.collected: list[dict] = []
        self.stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    # ── Bureau Collection ──────────────────────────────────────────────────

    async def collect_from_bureaus(self, limit: int = 0) -> None:
        """Collect environmental reports from provincial bureau websites.

        Crawls each bureau's '信息公开' or '通知公告' section for
        document links matching environmental report keywords.
        """
        data = load_json(ENV_BUREAUS_FILE)
        provinces = data.get("provinces", {})
        national = data.get("national", {})

        all_bureaus = list(provinces.items())
        if national:
            all_bureaus.insert(0, ("国家", national.get("国家", national)))

        count = 0
        for prov_name, bureau in all_bureaus:
            if not isinstance(bureau, dict):
                continue
            url = bureau.get("url", "")
            if not url:
                continue

            print(f"\n{'='*60}")
            print(f"🔍 [{prov_name}] {bureau.get('name', '')}")
            print(f"   {url}")

            count += 1
            if limit and count > limit:
                break

            # Collect from known sub-paths where reports are published
            await self._crawl_bureau(url, prov_name)

    async def _crawl_bureau(self, base_url: str, source: str) -> None:
        """Crawl a single bureau website for reports."""
        # Common paths for environmental report disclosures
        report_paths = [
            "/xxgk/",           # 信息公开
            "/zwgk/",           # 政务公开
            "/tzgg/",           # 通知公告
            "/gsgg/",           # 公示公告
            "/hjjg/",           # 环境监管
            "/xwzx/",           # 新闻中心
            "/hjzl/",           # 环境质量
        ]

        for path in report_paths:
            url = urljoin(base_url, path)
            try:
                self.stats["total"] += 1
                print(f"   → {url}")
                # Crawl4AI or httpx would go here
                # For now, log the intent
                self.collected.append({
                    "source": source,
                    "type": "bureau",
                    "url": url,
                    "status": "queued",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                print(f"   ❌ {url}: {e}")
                self.stats["failed"] += 1

            await asyncio.sleep(0.3)  # Rate limit

    # ── Chemical Park Collection ───────────────────────────────────────────

    async def collect_from_parks(self) -> None:
        """Collect enterprise lists from chemical industry parks."""
        parks = load_json(CHEMICAL_PARKS_FILE)
        if not isinstance(parks, list):
            return

        print(f"\n🔍 Collecting from {len(parks)} chemical parks...")

        for i, park in enumerate(parks):
            name = park.get("name", "")
            url = park.get("url", "")
            province = park.get("province", "")
            city = park.get("city", "")

            print(f"\n  [{i+1}/{len(parks)}] {name} ({province} {city})")
            print(f"     {url}")

            # Common enterprise list / entry registration paths
            entry_paths = [
                "/qyml/",       # 企业名录
                "/gsgg/",       # 公示公告
                "/tzgg/",       # 通知公告
                "/qyzc/",       # 企业注册
                "/ryyy/",       # 入园预约
            ]

            for path in entry_paths:
                full_url = urljoin(url, path)
                self.collected.append({
                    "source": name,
                    "source_type": "chemical_park",
                    "province": province,
                    "city": city,
                    "type": "enterprise_list",
                    "url": full_url,
                    "status": "queued",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            await asyncio.sleep(0.2)

    # ── Enterprise Search ──────────────────────────────────────────────────

    async def search_enterprise_reports(self, enterprise_name: str) -> None:
        """Search for environmental reports by enterprise name.

        Uses multiple search engines and document repositories:
        - 全国建设项目环境影响评价管理信息平台
        - 各省生态环境厅公示
        - 企查查/天眼查 (企业公开信息)
        """
        print(f"\n🔍 Searching: {enterprise_name}")

        # Search on national EIA platform
        eia_url = (
            "https://www.china-eia.com/"
            f"?keyword={enterprise_name}"
        )
        self.collected.append({
            "enterprise": enterprise_name,
            "type": "eia_search",
            "url": eia_url,
            "status": "queued",
        })

    # ── File Download ──────────────────────────────────────────────────────

    async def download_file(self, url: str, filename: Optional[str] = None) -> Optional[Path]:
        """Download a single file to the output directory."""
        file_type = classify_url(url)
        if not file_type:
            print(f"   ⚠️  Not a document: {url[:80]}")
            self.stats["skipped"] += 1
            return None

        if not filename:
            filename = f"{url_hash(url)}.{file_type}"

        filepath = self.output_dir / filename
        if filepath.exists():
            print(f"   ⏭️  Already downloaded: {filename}")
            self.stats["skipped"] += 1
            return filepath

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
                print(f"   ✅ {filename} ({len(resp.content) // 1024}KB)")
                self.stats["downloaded"] += 1
                return filepath
        except Exception as e:
            print(f"   ❌ {url[:80]}: {e}")
            self.stats["failed"] += 1
            return None

    # ── Export ─────────────────────────────────────────────────────────────

    def export_manifest(self) -> Path:
        """Export collected URLs and metadata as JSON manifest."""
        manifest_path = self.output_dir / "collector_manifest.json"
        manifest = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "stats": self.stats,
            "items": self.collected,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n📄 Manifest: {manifest_path} ({len(self.collected)} items)")
        return manifest_path


# ═══ CLI ════════════════════════════════════════════════════════════════════════


async def main():
    parser = argparse.ArgumentParser(
        description="Environmental Report Collector — 环境评估报告采集系统"
    )
    sub = parser.add_subparsers(dest="command")

    # bureaus
    sub.add_parser("bureaus", help="Collect from environmental bureaus")
    p_b = sub.add_parser("bureaus-sample", help="Sample first N bureaus")
    p_b.add_argument("--limit", type=int, default=5)

    # parks
    sub.add_parser("parks", help="Collect from chemical parks")

    # search
    p_s = sub.add_parser("search", help="Search enterprise reports")
    p_s.add_argument("name", help="Enterprise name")

    # full
    sub.add_parser("full", help="Full collection: bureaus + parks")

    args = parser.parse_args()
    collector = ReportCollector()

    if args.command == "bureaus":
        await collector.collect_from_bureaus()
    elif args.command == "bureaus-sample":
        await collector.collect_from_bureaus(limit=args.limit)
    elif args.command == "parks":
        await collector.collect_from_parks()
    elif args.command == "search":
        await collector.search_enterprise_reports(args.name)
    elif args.command == "full":
        await collector.collect_from_bureaus()
        await collector.collect_from_parks()
    else:
        parser.print_help()
        return

    collector.export_manifest()


if __name__ == "__main__":
    asyncio.run(main())
