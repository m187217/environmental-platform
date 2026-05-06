"""CLI tools for the environmental platform.

Usage:
    python -m src.cli crawl mee          # Run MEE spider
    python -m src.cli crawl cninfo       # Run CNINFO spider
    python -m src.cli process <file>     # Process a single document
    python -m src.cli process-batch <dir> # Batch process documents
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def cmd_crawl(args: argparse.Namespace) -> None:
    """Run a Scrapy spider."""
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings

    if args.spider == "mee":
        from src.crawler.spiders.mee_spider import MeeSpider
        spider_cls = MeeSpider
    elif args.spider == "cninfo":
        from src.crawler.spiders.cninfo_spider import CninfoSpider
        spider_cls = CninfoSpider
    else:
        print(f"Unknown spider: {args.spider}")
        sys.exit(1)

    settings = get_project_settings()
    settings.set("ITEM_PIPELINES", {
        "src.crawler.pipelines.DeduplicationPipeline": 100,
        "src.crawler.pipelines.DatabasePipeline": 200,
    })
    settings.set("DOWNLOADER_MIDDLEWARES", {
        "src.crawler.middleware.UserAgentRotationMiddleware": 400,
        "src.crawler.middleware.ThrottleMiddleware": 500,
        "src.crawler.middleware.RateLimitRetryMiddleware": 600,
    })

    process = CrawlerProcess(settings)
    process.crawl(spider_cls)
    process.start()


def cmd_process(args: argparse.Namespace) -> None:
    """Process a single document through the pipeline."""
    async def _run():
        from src.processor.pipeline import DocumentProcessor
        processor = DocumentProcessor()
        result = await processor.process(args.file)
        print(f"File: {result.file_name}")
        print(f"Stages: {result.stages_completed}")
        print(f"Time: {result.processing_time_ms:.0f}ms")
        print(f"Text length: {len(result.text_raw)} chars")
        if result.entities:
            print(f"Entities: {len(result.entities.entities)}")
            for e in result.entities.entities[:10]:
                print(f"  [{e.entity_type.value}] {e.text}")
        if result.errors:
            print(f"Errors: {result.errors}")

    asyncio.run(_run())


def cmd_process_batch(args: argparse.Namespace) -> None:
    """Batch process all documents in a directory."""
    async def _run():
        from src.processor.pipeline import DocumentProcessor
        processor = DocumentProcessor()
        docs = list(Path(args.dir).glob("*"))
        results = await processor.process_batch(docs)
        ok = sum(1 for r in results if not r.errors)
        fail = len(results) - ok
        print(f"Processed: {len(results)} docs ({ok} ok, {fail} failed)")
        total_time = sum(r.processing_time_ms for r in results)
        print(f"Total time: {total_time:.0f}ms")

    asyncio.run(_run())


def main() -> None:
    parser = argparse.ArgumentParser(description="Environmental Platform CLI")
    sub = parser.add_subparsers(dest="command")

    # crawl
    crawl_p = sub.add_parser("crawl", help="Run a crawler spider")
    crawl_p.add_argument("spider", choices=["mee", "cninfo"])
    crawl_p.add_argument("--days", type=int, help="Days to crawl (incremental)")

    # process
    proc_p = sub.add_parser("process", help="Process a single document")
    proc_p.add_argument("file", help="Path to document")

    # process-batch
    batch_p = sub.add_parser("process-batch", help="Batch process documents")
    batch_p.add_argument("dir", help="Directory containing documents")

    args = parser.parse_args()
    if args.command == "crawl":
        cmd_crawl(args)
    elif args.command == "process":
        cmd_process(args)
    elif args.command == "process-batch":
        cmd_process_batch(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
