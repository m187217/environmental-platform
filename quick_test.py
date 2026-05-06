# quick_test.py - 快速采集测试（仅广东）
import asyncio, sys, json
sys.path.insert(0, '/opt/environmental-platform')
from src.crawler.collector_v2 import ReportCollectorV2, fetcher
from pathlib import Path

async def test():
    collector = ReportCollectorV2()
    print("🚀 测试采集：广东省生态环境厅")
    print("=" * 50)
    
    url = "https://gdee.gd.gov.cn"
    reports = await collector.collect_bureau("广东省生态环境厅", url, "广东")
    
    print(f"\n📊 结果: {len(reports)} 条报告")
    for r in reports[:5]:
        print(f"  • {r['title'][:60]}")
        print(f"    URL: {r['url'][:80]}")
        print(f"    类型: {r['file_type']}")
    
    # 尝试下载第一个 PDF
    pdfs = [r for r in reports if r['file_type'] == 'pdf']
    if pdfs:
        print(f"\n📥 尝试下载第1个PDF...")
        fp = await collector.download_report(pdfs[0]['url'])
        if fp:
            print(f"  ✅ 下载成功: {fp}")
            print(f"  大小: {fp.stat().st_size} bytes")
    
    await fetcher.close()
    
    # 保存结果到data
    out = Path('/opt/environmental-platform/data/test_collect_result.json')
    out.write_text(json.dumps({
        "source": "广东-省厅",
        "url": url,
        "total": len(reports),
        "reports": reports[:20],
        "stats": collector.stats
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n📄 结果保存: {out}")

asyncio.run(test())
