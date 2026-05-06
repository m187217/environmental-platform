#!/usr/bin/env python3
from __future__ import annotations

"""test_eia_download.py — Test eiacloud.com judgeRole API and attachment download.
Tests:
1. judgeRole with userId='' (empty string) — free vs. paid
2. judgeRole with userId=null (None) — same comparison
3. Multiple publishIds from different categories
4. Extracts attachmentId from detail pages
5. Saves free downloads to /tmp/environmental_downloads/eiacloud/
6. Records download manifest to data/eia_downloads.json

Usage:
    python test_eia_download.py                    # Run all tests
    python test_eia_download.py --publish-id XXX   # Test specific publishId
    python test_eia_download.py --verbose           # Show full responses
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DOWNLOAD_DIR = Path("/tmp/environmental_downloads/eiacloud")
MANIFEST_FILE = DATA_DIR / "eia_downloads.json"

# Ensure directories exist
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://www.eiacloud.com"
JUDGE_ROLE_URL = "https://www.eiacloud.com/gs/judgeRole"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "X-Requested-With": "XMLHttpRequest",
}

# Rate limiting: 1 request per 2 seconds (as specified)
MIN_DELAY = 2.0

# ── Test Data ─────────────────────────────────────────────────────────────────

# Known publishIds from different categories (ring 评价公示 list pages)
# These are sample IDs extracted from the public listing pages
TEST_PUBLISH_IDS: list[dict[str, str]] = [
    {
        "id": "60506pQwZv",
        "title": "湛江经济技术开发区东海岛污水处理厂工程项目环境影响报告书报批前公示",
        "url": f"{BASE_URL}/gs/detail/1?id=60506pQwZv",
    },
    {
        "id": "60505pQwZu",
        "title": "广州市花都区生活垃圾填埋场封场项目环境影响报告书",
        "url": f"{BASE_URL}/gs/detail/1?id=60505pQwZu",
    },
    {
        "id": "60504pQwZt",
        "title": "佛山高明区更合镇某金属表面处理项目",
        "url": f"{BASE_URL}/gs/detail/1?id=60504pQwZt",
    },
]

# More test IDs from different provinces/sectors
EXTRA_TEST_IDS: list[dict[str, str]] = [
    {
        "id": "60500pQwZp",
        "title": "江苏省某化工园区规划环境影响报告书",
        "url": f"{BASE_URL}/gs/detail/1?id=60500pQwZp",
    },
    {
        "id": "60495pQwZk",
        "title": "山东省某造纸企业技改项目环境影响报告书",
        "url": f"{BASE_URL}/gs/detail/1?id=60495pQwZk",
    },
]


# ── Helper Functions ──────────────────────────────────────────────────────────


def load_manifest() -> dict[str, Any]:
    """Load existing download manifest from disk."""
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text("utf-8"))
    return {"version": "1.0", "downloads": [], "stats": {"total": 0, "free": 0, "paid": 0, "failed": 0}}


def save_manifest(manifest: dict[str, Any]) -> None:
    """Save download manifest to disk."""
    MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


_last_request_time: float = 0.0


def rate_limit():
    """Enforce minimum delay between requests."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < MIN_DELAY:
        sleep_time = MIN_DELAY - elapsed
        print(f"    ⏳ Rate limit: sleeping {sleep_time:.1f}s...")
        time.sleep(sleep_time)
    _last_request_time = time.monotonic()


def judge_role(
    publish_id: str,
    attachment_id: str,
    user_id: str | None = "",
    source: int = 0,
) -> dict[str, Any]:
    """Call the judgeRole API to check if an attachment is downloadable.

    Args:
        publish_id: The gkPublishId
        attachment_id: The attachmentId
        user_id: Empty string '' for anonymous, None for no userId
        source: 0 for normal

    Returns:
        Parsed JSON response from the server.
    """
    rate_limit()

    payload: dict[str, Any] = {
        "id": publish_id,
        "attachmentId": attachment_id,
        "userId": user_id,
        "source": source,
    }

    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        resp = client.post(
            JUDGE_ROLE_URL,
            json=payload,
            headers=HEADERS,
        )

    result: dict[str, Any] = {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": {},
        "is_redirect": resp.is_redirect or resp.status_code in (301, 302),
        "location": str(resp.headers.get("location", "")),
    }

    # Try to parse as JSON
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type or resp.text.startswith("{"):
        try:
            result["body"] = resp.json()
        except ValueError:
            result["body"] = {"raw": resp.text[:1000]}
    else:
        result["body"] = {"raw": resp.text[:1000]}

    result["is_free"] = (
        resp.status_code == 200
        and result["body"].get("code") == 200
        and bool(result["body"].get("data") or result["location"])
    )

    return result


def fetch_detail_page(publish_id: str) -> str | None:
    """Fetch the detail page HTML for a given publish ID.

    Returns:
        HTML string, or None on failure.
    """
    rate_limit()

    url = f"{BASE_URL}/gs/detail/1?id={publish_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"{BASE_URL}/",
    }

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.text
            else:
                print(f"    ❌ HTTP {resp.status_code} fetching detail page for {publish_id}")
                return None
    except Exception as e:
        print(f"    ❌ Error fetching detail page: {e}")
        return None


def extract_attachment_ids(html: str) -> list[dict[str, str]]:
    """Extract attachment IDs and filenames from a detail page HTML.

    In eiacloud, attachments are rendered with .down-publish-att elements
    that have data-attachment-id attribute.

    Returns:
        List of dicts with 'attachment_id' and 'file_name' keys.
    """
    soup = BeautifulSoup(html, "lxml")
    attachments: list[dict[str, str]] = []

    # Method 1: Find .down-publish-att elements with data attributes
    for el in soup.select(".down-publish-att"):
        att_id = el.get("data-attachment-id") or el.get("data-id") or ""
        file_name = el.get("data-name") or el.get("title") or el.get_text(strip=True)
        if att_id:
            attachments.append({
                "attachment_id": str(att_id).strip(),
                "file_name": str(file_name).strip() if file_name else "unknown.pdf",
            })

    # Method 2: Look for attachment links in the DOM
    if not attachments:
        for el in soup.select("a[href*='attachment' i], a[data-attachment-id], [data-attachment-id]"):
            att_id = el.get("data-attachment-id") or ""
            href = el.get("href", "")
            file_name = el.get("data-name") or el.get("title") or el.get_text(strip=True)
            if att_id:
                attachments.append({
                    "attachment_id": str(att_id).strip(),
                    "file_name": str(file_name).strip() if file_name else Path(href).name or "unknown.pdf",
                })

    # Method 3: Parse inline JavaScript data
    if not attachments:
        for script in soup.find_all("script"):
            text = script.string or ""
            if "attachmentId" in text or "attachment" in text.lower():
                import re
                # Try to find data attachments embedded in JS
                matches = re.findall(
                    r'["\']?attachment(?:Id|id)?["\']?\s*[:=]\s*["\']([a-f0-9]{32})["\']',
                    text,
                )
                for m in matches:
                    attachments.append({
                        "attachment_id": m,
                        "file_name": "unknown.pdf",
                    })

    return attachments


def download_file(download_url: str, save_path: Path) -> bool:
    """Download a file from URL to local path.

    Args:
        download_url: URL to download from
        save_path: Local path to save to

    Returns:
        True if download succeeded, False otherwise.
    """
    rate_limit()

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": f"{BASE_URL}/",
    }

    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            resp = client.get(download_url, headers=headers)
            if resp.status_code == 200 and len(resp.content) > 1024:
                save_path.write_bytes(resp.content)
                return True
            else:
                print(f"    ❌ Download failed: HTTP {resp.status_code}, size={len(resp.content)}")
                return False
    except Exception as e:
        print(f"    ❌ Download error: {e}")
        return False


# ── Test Functions ────────────────────────────────────────────────────────────


def test_judge_role_api(publish_id: str, verbose: bool = False) -> dict[str, Any]:
    """Test judgeRole API with both userId='' and userId=None.

    Returns a dict with test results.
    """
    print(f"\n{'='*70}")
    print(f"📋 Testing publishId: {publish_id}")
    print(f"{'='*70}")

    # First, fetch detail page to get attachment IDs
    print(f"\n  🔍 Fetching detail page...")
    html = fetch_detail_page(publish_id)
    if not html:
        return {"publish_id": publish_id, "error": "Failed to fetch detail page"}

    attachments = extract_attachment_ids(html)
    print(f"  📎 Found {len(attachments)} attachment(s)")

    results: list[dict[str, Any]] = []

    for i, att in enumerate(attachments):
        att_id = att["attachment_id"]
        file_name = att["file_name"]
        print(f"\n  ── Attachment #{i+1}: {att_id} ({file_name}) ──")

        # Test with userId=''
        print(f"    🔬 Testing userId='' (empty string)...")
        res_empty = judge_role(publish_id, att_id, user_id="")
        is_free_empty = res_empty["is_free"]

        if verbose:
            print(f"      Status: {res_empty['status_code']}, Body: {json.dumps(res_empty['body'], ensure_ascii=False)[:300]}")
        else:
            code = res_empty["body"].get("code", "N/A")
            msg = res_empty["body"].get("msg", res_empty["body"].get("message", ""))
            print(f"      → code={code}, is_free={is_free_empty}, msg={msg[:100] if msg else 'N/A'}")

        # Test with userId=None
        print(f"    🔬 Testing userId=null...")
        res_null = judge_role(publish_id, att_id, user_id=None)
        is_free_null = res_null["is_free"]

        if verbose:
            print(f"      Status: {res_null['status_code']}, Body: {json.dumps(res_null['body'], ensure_ascii=False)[:300]}")
        else:
            code = res_null["body"].get("code", "N/A")
            msg = res_null["body"].get("msg", res_null["body"].get("message", ""))
            print(f"      → code={code}, is_free={is_free_null}, msg={msg[:100] if msg else 'N/A'}")

        # Get download URL if free
        download_url = ""
        if is_free_empty:
            # Try to get download URL from response
            data = res_empty["body"].get("data", "")
            if isinstance(data, str) and data.startswith("http"):
                download_url = data
            elif res_empty["location"]:
                download_url = res_empty["location"]
            print(f"      ✅ FREE DOWNLOAD AVAILABLE!")
        elif is_free_null:
            data = res_null["body"].get("data", "")
            if isinstance(data, str) and data.startswith("http"):
                download_url = data
            elif res_null["location"]:
                download_url = res_null["location"]
            print(f"      ✅ FREE (with userId=null)!")

        att_result = {
            "attachment_id": att_id,
            "file_name": file_name,
            "user_id_empty": {
                "status_code": res_empty["status_code"],
                "code": res_empty["body"].get("code", ""),
                "msg": res_empty["body"].get("msg", res_empty["body"].get("message", "")),
                "is_free": is_free_empty,
            },
            "user_id_null": {
                "status_code": res_null["status_code"],
                "code": res_null["body"].get("code", ""),
                "msg": res_null["body"].get("msg", res_null["body"].get("message", "")),
                "is_free": is_free_null,
            },
            "download_url": download_url,
        }

        # Actually download if free
        if download_url:
            safe_filename = f"{publish_id}_{file_name or att_id}"
            # Clean filename
            safe_filename = "".join(c for c in safe_filename if c.isalnum() or c in "._- ").strip()
            if not safe_filename.endswith((".pdf", ".docx", ".doc", ".zip")):
                safe_filename += ".pdf"
            save_path = DOWNLOAD_DIR / safe_filename

            print(f"    ⬇️  Downloading to {save_path}...")
            success = download_file(download_url, save_path)
            if success:
                size_mb = save_path.stat().st_size / (1024 * 1024)
                print(f"      ✅ Downloaded: {size_mb:.1f} MB")
                att_result["downloaded"] = True
                att_result["save_path"] = str(save_path)
                att_result["file_size"] = save_path.stat().st_size
            else:
                print(f"      ❌ Download failed")
                att_result["downloaded"] = False
        else:
            att_result["downloaded"] = False

        results.append(att_result)

    return {
        "publish_id": publish_id,
        "title": "",  # Will fill from detail page
        "attachments": results,
        "free_count": sum(1 for r in results if r["user_id_empty"]["is_free"] or r["user_id_null"]["is_free"]),
        "total_attachments": len(results),
    }


def extract_title_from_html(html: str) -> str:
    """Extract the report title from detail page HTML."""
    soup = BeautifulSoup(html, "lxml")
    # Try multiple selectors for title
    for selector in ["h1", ".detail-title", ".title", "title"]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return ""


def main():
    parser = argparse.ArgumentParser(description="Test eiacloud.com judgeRole API and download files")
    parser.add_argument("--publish-id", type=str, help="Test a specific publish ID only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full API responses")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between requests in seconds")
    parser.add_argument("--output", type=str, default=str(MANIFEST_FILE), help="Output manifest file path")
    args = parser.parse_args()

    global MIN_DELAY
    if args.delay:
        MIN_DELAY = args.delay

    output_path = Path(args.output)

    print("=" * 70)
    print("🧪 eiacloud.com 下载测试脚本")
    print("=" * 70)
    print(f"\nRate limit: {MIN_DELAY}s between requests")
    print(f"Download dir: {DOWNLOAD_DIR}")
    print(f"Manifest: {output_path}")

    # Determine which IDs to test
    if args.publish_id:
        test_ids = [{"id": args.publish_id, "title": "", "url": f"{BASE_URL}/gs/detail/1?id={args.publish_id}"}]
    else:
        test_ids = TEST_PUBLISH_IDS + EXTRA_TEST_IDS

    all_results: list[dict[str, Any]] = []

    for item in test_ids:
        pid = item["id"]
        title = item.get("title", "")

        result = test_judge_role_api(pid, verbose=args.verbose)

        # Try to get title from detail page if not provided
        html = fetch_detail_page(pid)
        if html:
            extracted_title = extract_title_from_html(html)
            if extracted_title:
                result["title"] = extracted_title
        if not result.get("title") and title:
            result["title"] = title

        all_results.append(result)

        # Summary per publish ID
        print(f"\n  📊 Summary for {pid}:")
        print(f"     Title: {result.get('title', 'N/A')[:80]}")
        print(f"     Attachments: {result['total_attachments']}, Free: {result['free_count']}")

    # ── Global Summary ────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("📊 全局测试总结")
    print(f"{'='*70}")

    total_attachments = sum(r["total_attachments"] for r in all_results)
    total_free = sum(r["free_count"] for r in all_results)
    total_paid = total_attachments - total_free

    print(f"  测试的报告数: {len(all_results)}")
    print(f"  总的附件数: {total_attachments}")
    print(f"  可免费下载: {total_free}")
    print(f"  需付费: {total_paid}")
    print(f"  免费率: {total_free/total_attachments*100:.1f}%" if total_attachments > 0 else "  N/A")

    # Save manifest
    manifest = load_manifest()
    timestamp = datetime.now(timezone.utc).isoformat()

    for result in all_results:
        manifest["downloads"].append({
            "publish_id": result["publish_id"],
            "title": result.get("title", ""),
            "timestamp": timestamp,
            "attachments": result["attachments"],
            "free_count": result["free_count"],
            "total_attachments": result["total_attachments"],
        })

    manifest["stats"]["total"] += total_attachments
    manifest["stats"]["free"] += total_free
    manifest["stats"]["paid"] += total_paid
    manifest["stats"]["last_test"] = timestamp
    manifest["stats"]["reports_tested"] = manifest["stats"].get("reports_tested", 0) + len(all_results)

    save_manifest(manifest)
    print(f"\n  ✅ Manifest saved to {output_path}")
    print(f"  📁 Downloads in: {DOWNLOAD_DIR}")

    # List downloaded files
    downloaded_files = list(DOWNLOAD_DIR.iterdir()) if DOWNLOAD_DIR.exists() else []
    if downloaded_files:
        print(f"\n  📦 Downloaded files ({len(downloaded_files)}):")
        for f in sorted(downloaded_files)[:10]:
            size = f.stat().st_size / 1024
            print(f"     - {f.name} ({size:.0f} KB)")
        if len(downloaded_files) > 10:
            print(f"     ... and {len(downloaded_files)-10} more")


if __name__ == "__main__":
    main()
