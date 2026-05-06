"""Enterprise Environmental Report Search Engine

Searches for environmental assessment reports by enterprise name across:
  1. 全国建设项目环境影响评价管理信息平台 (china-eia.com)
  2. 各省生态环境厅公示页面
  3. 百度/Google 搜索限定 PDF 文件

Usage:
    python -m src.crawler.enterprise_search 万华化学
    python -m src.crawler.enterprise_search --batch enterprises.txt
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/environmental_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (compatible; EnvironmentalBot/2.0; +https://github.com/m187217/environmental-platform)"

# Known EIA disclosure platforms
EIA_PLATFORMS = [
    {
        "name": "全国环评管理信息平台",
        "url": "http://114.251.10.205:8080/XYPT/",
        "search_pattern": "/XYPT/front/sendRequest?key={keyword}",
    },
    {
        "name": "广东省环评公示",
        "url": "http://gdee.gd.gov.cn/",
        "search_path": "/showservice/",
    },
    {
        "name": "浙江省环评公示",
        "url": "http://sthjt.zj.gov.cn/",
        "search_path": "/col/col1229559241/",
    },
]

EXCLUDE_DOMAINS = {"facebook.com", "twitter.com", "instagram.com", "youtube.com", "linkedin.com"}


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


async def search_enterprise(enterprise_name: str) -> list[dict]:
    """Search for environmental reports of a given enterprise.

    Returns list of {title, url, source, file_type} dicts.
    """
    import httpx
    results = []

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30),
        follow_redirects=True,
        verify=False,
    ) as client:

        # ── Strategy 1: Direct EIA platform queries ──
        for platform in EIA_PLATFORMS:
            try:
                search_url = platform["url"] + platform.get("search_path", "")
                resp = await client.get(search_url, params={"q": enterprise_name})
                if resp.status_code == 200 and len(resp.text) > 500:
                    # Extract links
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "lxml")
                    for a in soup.find_all("a", href=True):
                        title = a.get_text(strip=True)
                        if any(kw in title for kw in ["环评", "报告", "公示", enterprise_name]):
                            full_url = urljoin(str(resp.url), a["href"])
                            results.append({
                                "enterprise": enterprise_name,
                                "title": title,
                                "url": full_url,
                                "source": platform["name"],
                                "source_url": str(resp.url),
                            })
            except Exception:
                continue

        # ── Strategy 2: Search with encoded enterprise name ──
        search_queries = [
            f"{enterprise_name} 环境影响评价报告",
            f"{enterprise_name} 环评报告书 filetype:pdf",
            f"{enterprise_name} 竣工环保验收",
        ]

        for query in search_queries[:2]:  # Limit to 2 queries
            try:
                # Use DuckDuckGo to search
                from duckduckgo_search import DDGS
                ddgs = DDGS()
                hits = list(ddgs.text(query, max_results=5))
                for h in hits:
                    href = h.get("href", "")
                    if any(d in href for d in EXCLUDE_DOMAINS):
                        continue
                    results.append({
                        "enterprise": enterprise_name,
                        "title": h.get("title", ""),
                        "url": href,
                        "source": "ddg_search",
                        "source_url": query,
                    })
            except Exception:
                pass

            await asyncio.sleep(2)

    return results


async def search_batch(enterprises: list[str]) -> dict[str, list[dict]]:
    """Search multiple enterprises in parallel batches."""
    all_results = {}
    sem = asyncio.Semaphore(5)

    async def _search_one(name: str):
        async with sem:
            results = await search_enterprise(name)
            if results:
                all_results[name] = results
                print(f"  {name}: {len(results)} reports found")

    tasks = [_search_one(name) for name in enterprises]
    await asyncio.gather(*tasks)
    return all_results


async def download_report(url: str, filename: str = "") -> Optional[Path]:
    """Download a report file from URL."""
    import httpx

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(60),
        follow_redirects=True,
        verify=False,
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()

            # Determine file type from content or URL
            content_type = resp.headers.get("content-type", "")
            if "pdf" in content_type:
                ext = "pdf"
            elif "word" in content_type or "docx" in content_type:
                ext = "docx"
            elif url.lower().endswith(".pdf"):
                ext = "pdf"
            elif url.lower().endswith(".docx"):
                ext = "docx"
            else:
                ext = "bin"

            if not filename:
                filename = f"{url_hash(url)}.{ext}"

            filepath = DOWNLOAD_DIR / filename
            filepath.write_bytes(resp.content)
            return filepath

        except Exception as e:
            return None


# ═══ CLI ═══


async def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.crawler.enterprise_search <enterprise_name>")
        print("       python -m src.crawler.enterprise_search --batch <file.txt>")
        return

    if sys.argv[1] == "--batch":
        batch_file = Path(sys.argv[2])
        if not batch_file.exists():
            print(f"File not found: {batch_file}")
            return
        enterprises = batch_file.read_text().strip().split("\n")
        print(f"Searching {len(enterprises)} enterprises...")
        results = await search_batch(enterprises)
    else:
        name = " ".join(sys.argv[1:])
        print(f"Searching: {name}")
        results = {name: await search_enterprise(name)}

    # Save results
    output_file = DOWNLOAD_DIR / f"enterprise_search_{datetime.now():%Y%m%d_%H%M%S}.json"
    output_file.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nSaved to: {output_file}")

    # Summary
    total = sum(len(v) for v in results.values())
    print(f"Total: {total} reports across {len(results)} enterprises")


if __name__ == "__main__":
    asyncio.run(main())
