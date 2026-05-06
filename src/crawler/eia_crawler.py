#!/usr/bin/env python3
"""eiacloud.com 环境信息公示平台元数据爬虫.

抓取 eiacloud.com 上的环评报告、验收报告等公示信息，
解析列表页和详情页的结构化字段，
保存到 JSON 文件、PostgreSQL 和 Elasticsearch。

Usage:
    python3 src/crawler/eia_crawler.py --category 1 --pages 2
    python3 src/crawler/eia_crawler.py --category 2 --pages 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

# ═══ Project path setup ═══
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("eia_crawler")

# ═══ Constants ═══
BASE_URL = "https://www.eiacloud.com"
LIST_URL_TEMPLATE = f"{BASE_URL}/gs/list/{{category}}?pageModel.numberNo={{page}}"
DETAIL_URL_TEMPLATE = f"{BASE_URL}/gs/detail/{{category}}?id={{publish_id}}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

DATA_DIR = PROJECT_ROOT / "data"
EIA_JSON_PATH = DATA_DIR / "eia_metadata.json"

# ═══ Category name mapping ═══
CATEGORY_NAMES = {
    1: "环评报告公示",
    2: "验收报告公示",
    3: "公众参与公示",
    4: "土壤与地下水调查公示",
    5: "固废信息公开",
    6: "水保验收公示",
    7: "环境应急预案公示",
    8: "企业环境信息披露",
    9: "建设单位信息公开",
    10: "清洁生产审核公示",
    11: "排污单位信息披露",
    12: "环评审批前公示",
    13: "机构合作展示",
    14: "问题反馈与求助",
}


# ═══ Helpers ═══


def make_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def extract_list_items(html: str) -> list[dict[str, Any]]:
    """Parse the list page HTML and extract basic metadata for each item."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []

    # The data is in <tr> elements inside a <table>
    rows = soup.find_all("tr")
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 4:
            continue

        # Skip header row
        if "table_title" in (tds[0].get("class", "") or ""):
            continue

        # TD[0]: icon with link to detail
        icon_td = tds[0]
        detail_link = icon_td.find("a", href=True)
        if not detail_link:
            continue
        detail_href = detail_link["href"]  # e.g. /gs/detail/1?id=21010yzwak
        detail_url = urljoin(BASE_URL, detail_href)

        # Extract publish_id from detail URL
        publish_id_match = re.search(r"id=([\w]+)", detail_href)
        if not publish_id_match:
            continue
        publish_id = publish_id_match.group(1)

        # TD[1]: title + attachement count + location + status
        title_td = tds[1]

        # -- title link --
        title_link = title_td.find("a", class_="f_title")
        title = title_link.get_text(strip=True) if title_link else ""

        # -- category tag (e.g. [一次], [二次], [其它]) --
        category_tag = ""
        em_tag = title_td.find("em")
        if em_tag:
            cat_text = em_tag.get_text(strip=True)
            category_tag = cat_text.strip("[]")

        # -- attachment indicator --
        attachment_count = 0
        att_div = title_td.find("div", class_="fct_att")
        if att_div:
            # Extract number after "附件" text
            att_text = att_div.get_text(strip=True)
            att_match = re.search(r"(\d+)", att_text)
            if att_match:
                attachment_count = int(att_match.group(1))

        # -- location --
        location = ""
        bottom_div = title_td.find("div", class_="f_common_bottom")
        if bottom_div:
            loc_div = bottom_div.find("div", class_="fcb-location")
            if loc_div:
                location = loc_div.get_text(strip=True)

        # -- status text --
        status = ""
        if bottom_div:
            status_span = bottom_div.find("span", class_="gsz")
            if status_span:
                status = status_span.get_text(strip=True)

        # TD[2]: latest reply time
        reply_td = tds[2]
        reply_time = ""
        time_span = reply_td.find("span", class_="f_time")
        if time_span:
            reply_time = time_span.get_text(strip=True)

        # TD[3]: download/view count
        num_td = tds[3]
        num_spans = num_td.find_all("span")
        download_count = "0"
        view_count = "0"
        if len(num_spans) >= 2:
            download_count = num_spans[0].get_text(strip=True)
            view_count = num_spans[1].get_text(strip=True)

        # TD[4]: author + publish time
        author_td = tds[4]
        author = ""
        pub_time = ""
        author_link = author_td.find("a", class_="f_peo")
        if author_link:
            author = author_link.get_text(strip=True)
        pub_time_span = author_td.find("span", class_="f_time")
        if pub_time_span:
            pub_time = pub_time_span.get_text(strip=True)

        items.append({
            "publish_id": publish_id,
            "detail_url": detail_url,
            "title": title,
            "category_tag": category_tag,
            "attachment_count": attachment_count,
            "location": location,
            "status": status,
            "reply_time": reply_time,
            "download_count": download_count,
            "view_count": view_count,
            "author": author,
            "publish_time": pub_time,
        })

    return items


def parse_detail_page(html: str) -> dict[str, Any]:
    """Parse the detail page HTML and extract structured fields."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, Any] = {}

    # ── Project info from left sidebar (project_ul) ──
    project_ul = soup.find("ul", class_="project_ul")
    if project_ul:
        items = project_ul.find_all("li")
        for li in items:
            label_div = li.find("div", class_="project_li_l")
            value_div = li.find("div", class_="project_li_r")
            if label_div and value_div:
                label = label_div.get_text(strip=True)
                value = value_div.get_text(strip=True)
                if label == "项目名称":
                    result["project_name"] = value
                elif label == "报告类型":
                    result["report_type"] = value
                elif label == "行业分类":
                    result["industry"] = value
                elif label == "项目位置":
                    result["location_detail"] = value
                elif label == "项目性质":
                    result["project_nature"] = value
                elif label == "公示状态":
                    result["status_detail"] = value
                elif label == "公示有效期":
                    result["valid_period"] = value

    # ── Main content area (detail_text / detail_main) ──
    content_div = soup.find("div", id="contentContainer")
    if not content_div:
        content_div = soup.find("div", class_="detail_main")
    if not content_div:
        content_div = soup.find("div", class_="detail_text")

    if content_div:
        # Extract structured fields from content text
        content_text = content_div.get_text(separator="\n", strip=True)

        # Try to extract company name (建设单位)
        company_match = re.search(
            r"建设单位[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if company_match:
            result["company"] = company_match.group(1).strip()

        # Try to extract project name from content
        proj_match = re.search(
            r"建设项目名称[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if proj_match:
            result["project_name_from_content"] = proj_match.group(1).strip()

        # Try to extract construction location
        location_match = re.search(
            r"建设地点[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if location_match:
            result["construction_location"] = location_match.group(1).strip()

        # Try to extract construction content
        content_match = re.search(
            r"建设内容[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if content_match:
            result["construction_content"] = content_match.group(1).strip()

        # Try to extract contact person
        contact_match = re.search(
            r"联系人[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if contact_match:
            result["contact_person"] = contact_match.group(1).strip()

        # Try to extract phone
        phone_match = re.search(
            r"联系电话[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if phone_match:
            result["phone"] = phone_match.group(1).strip()

        # Try to extract email
        email_match = re.search(
            r"邮箱[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if email_match:
            result["email"] = email_match.group(1).strip()

        # Try to extract EIA unit (环评单位)
        eia_unit_match = re.search(
            r"环评单位[：:]\s*(.+?)(?:\n|$)", content_text
        )
        if eia_unit_match:
            result["eia_unit"] = eia_unit_match.group(1).strip()

        result["description"] = content_text

    # ── Title from the detail page ──
    title_span = soup.find("span", class_="box_tit")
    if title_span:
        result["detail_title"] = title_span.get_text(strip=True)

    # ── Attachments ──
    fj_div = soup.find("div", class_="detail_fj")
    attachments: list[dict[str, Any]] = []
    if fj_div:
        attachment_links = fj_div.find_all("a", class_="down-publish-att")
        for a in attachment_links:
            attach_id = a.get("data", "")
            attach_name = a.get_text(strip=True)

            # Find size span next to the link
            size_span = a.find_next_sibling("span")
            size = size_span.get_text(strip=True) if size_span else ""

            attachments.append({
                "attachment_id": attach_id,
                "name": attach_name,
                "size": size,
            })

    result["attachments"] = attachments

    # ── Author / poster info from top ──
    top_details = soup.find("div", class_="top_details")
    if top_details:
        author_text = top_details.get_text(strip=True)
        # e.g. "###发表于 2026-05-06 23:12"
        author_match = re.match(r"(.+?)发表于", author_text)
        if author_match:
            result["post_author"] = author_match.group(1).strip()
        time_match = re.search(r"发表于\s*([\d\-:\s]+)", author_text)
        if time_match:
            result["post_time"] = time_match.group(1).strip()

    return result


# ═══ Database & ES operations ═══


def init_db() -> Any:
    """Connect to PostgreSQL."""
    try:
        import psycopg2
        db_url = os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/environmental",
        )
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        logger.warning("PostgreSQL connection failed: %s", e)
        return None


def save_to_db(
    conn: Any, item: dict[str, Any], url_hash: str
) -> bool:
    """Save a record to PostgreSQL reports table. Returns True if inserted."""
    if not conn:
        return False

    try:
        cur = conn.cursor()

        # Check if url_hash already exists
        cur.execute(
            "SELECT id FROM reports WHERE url_hash = %s", (url_hash,)
        )
        if cur.fetchone():
            return False  # duplicate

        url = item.get("detail_url", "")
        title = item.get("detail_title") or item.get("title", "")
        source = "eiacloud"
        section = item.get("category_tag", "")
        published_at = None
        pub_time = item.get("post_time") or item.get("publish_time", "")
        if pub_time:
            try:
                published_at = datetime.strptime(
                    pub_time.strip(), "%Y-%m-%d %H:%M"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        raw_meta = json.dumps(item, ensure_ascii=False)

        cur.execute(
            """INSERT INTO reports
               (url_hash, url, title, source, section, published_at, scraped_at, raw_meta)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (
                url_hash,
                url,
                title,
                source,
                section,
                published_at,
                datetime.now(timezone.utc),
                raw_meta,
            ),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.warning("DB save error: %s", e)
        conn.rollback()
        return False


def index_to_es(item: dict[str, Any]) -> bool:
    """Index the record to Elasticsearch."""
    try:
        from elasticsearch import Elasticsearch

        es_url = os.getenv(
            "ELASTICSEARCH_URL", "http://localhost:9200"
        )
        es = Elasticsearch(es_url)

        title = item.get("detail_title") or item.get("title", "")
        description = item.get("description", "")
        content = f"{title}\n{description}"

        doc = {
            "title": title,
            "content": content,
            "source": "eiacloud",
            "url": item.get("detail_url", ""),
            "category": item.get("category_tag", ""),
            "company": item.get("company", ""),
            "project_name": item.get("project_name", ""),
            "location": item.get("location_detail", "")
            or item.get("construction_location", ""),
            "industry": item.get("industry", ""),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

        # Use url as document ID for dedup
        doc_id = make_hash(item.get("detail_url", ""))
        es.index(index="reports", id=doc_id, body=doc)
        return True
    except Exception as e:
        logger.warning("ES index error: %s", e)
        return False


# ═══ Main crawler ═══


class EiaCrawler:
    """Crawler for eiacloud.com environmental information platform."""

    def __init__(
        self,
        category: int = 1,
        pages: int = 2,
        delay: float = 0.5,
    ):
        self.category = category
        self.pages = pages
        self.delay = delay
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )

        # Stats
        self.fetched_count = 0
        self.duplicate_count = 0
        self.db_inserted_count = 0
        self.es_inserted_count = 0
        self.errors: list[str] = []

        # Dedup tracking
        self.seen_hashes: set[str] = set()

        # DB connection
        self.db_conn = init_db()

        # Results
        self.results: list[dict[str, Any]] = []

    def run(self) -> None:
        """Execute the crawl."""
        category_name = CATEGORY_NAMES.get(
            self.category, f"category_{self.category}"
        )
        logger.info(
            "Starting crawl: category=%d (%s), pages=%d",
            self.category,
            category_name,
            self.pages,
        )

        for page in range(1, self.pages + 1):
            logger.info("Fetching list page %d/%d...", page, self.pages)
            items = self._fetch_list_page(page)
            if not items:
                logger.warning(
                    "No items found on page %d, stopping.", page
                )
                break

            logger.info(
                "Found %d items on page %d", len(items), page
            )

            for item in items:
                self._process_item(item)
                time.sleep(self.delay)

        self._save_results()
        self._print_stats()

    def _fetch_list_page(self, page: int) -> list[dict[str, Any]]:
        """Fetch and parse a single list page."""
        url = LIST_URL_TEMPLATE.format(
            category=self.category, page=page
        )
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            items = extract_list_items(resp.text)
            return items
        except Exception as e:
            logger.error(
                "Failed to fetch list page %d: %s", page, e
            )
            self.errors.append(
                f"List page {page}: {e!s}"
            )
            return []

    def _process_item(self, list_item: dict[str, Any]) -> None:
        """Process a single list item: fetch detail, parse, save."""
        publish_id = list_item["publish_id"]
        detail_url = list_item["detail_url"]

        # Dedup by URL
        url_hash = make_hash(detail_url)
        if url_hash in self.seen_hashes:
            self.duplicate_count += 1
            return
        self.seen_hashes.add(url_hash)
        self.fetched_count += 1

        # Fetch detail page
        try:
            resp = self.client.get(detail_url)
            resp.raise_for_status()
            detail_data = parse_detail_page(resp.text)
        except Exception as e:
            logger.error(
                "Failed to fetch detail for %s: %s",
                publish_id,
                e,
            )
            self.errors.append(
                f"Detail {publish_id}: {e!s}"
            )
            detail_data = {}

        # Merge list and detail data
        merged = {**list_item, **detail_data}

        # Save to JSON results
        self.results.append(merged)

        # Save to PostgreSQL
        inserted = save_to_db(self.db_conn, merged, url_hash)
        if inserted:
            self.db_inserted_count += 1
        else:
            self.duplicate_count += 1

        # Index to ES
        indexed = index_to_es(merged)
        if indexed:
            self.es_inserted_count += 1

        logger.debug(
            "Processed: %s | company=%s | project=%s",
            merged.get("detail_title", merged.get("title", "")),
            merged.get("company", ""),
            merged.get("project_name", ""),
        )

    def _save_results(self) -> None:
        """Save all results to JSON file."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(EIA_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(
                self.results,
                f,
                ensure_ascii=False,
                indent=2,
            )
        logger.info(
            "Saved %d records to %s",
            len(self.results),
            EIA_JSON_PATH,
        )

    def _print_stats(self) -> None:
        """Print crawl statistics."""
        print("\n" + "=" * 60)
        print(f"  eiacloud 爬虫统计 — 分类 {self.category} ({CATEGORY_NAMES.get(self.category, '未知')})")
        print("=" * 60)
        print(f"  抓取数（从列表页获取）:  {self.fetched_count}")
        print(f"  重复数（去重过滤）:      {self.duplicate_count}")
        print(f"  入库数（PostgreSQL）:    {self.db_inserted_count}")
        print(f"  索引数（Elasticsearch）: {self.es_inserted_count}")
        print(f"  保存到 JSON:            {len(self.results)} 条")
        print(f"  错误数:                 {len(self.errors)}")
        if self.errors:
            print("  错误详情:")
            for err in self.errors[:10]:
                print(f"    - {err}")
        print("=" * 60)

    def close(self) -> None:
        """Clean up resources."""
        self.client.close()
        if self.db_conn:
            self.db_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="eiacloud.com 环境信息公示平台爬虫"
    )
    parser.add_argument(
        "--category",
        type=int,
        default=1,
        help="分类ID (1=环评报告公示, 2=验收报告公示, 等)",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="抓取页数 (每页20条)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="请求间隔秒数 (默认0.5)",
    )
    args = parser.parse_args()

    crawler = EiaCrawler(
        category=args.category,
        pages=args.pages,
        delay=args.delay,
    )
    try:
        crawler.run()
    finally:
        crawler.close()


if __name__ == "__main__":
    main()
