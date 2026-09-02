"""Crawl completo de Avellaneda con caudal adaptativo.

  uv run python scripts_ava_crawl.py --calibrate      # busca el techo con 600 fichas
  uv run python scripts_ava_crawl.py                  # crawl completo (reanudable)
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, "src")
from fadia.adapters.avellaneda import AvellanedaAdapter
from fadia.crawl import BulkCrawler
from fadia.fetch import Fetcher
from fadia.models import Store

OUT = Path("data/avellaneda")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true",
                    help="corrida corta para encontrar el techo del servidor")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-concurrency", type=int, default=32)
    ap.add_argument("--start-concurrency", type=int, default=4)
    a = ap.parse_args()

    rows = list(csv.DictReader((OUT / "inventory.csv").open(encoding="utf-8")))
    urls = [r["url"] for r in rows]
    if a.calibrate:
        urls = urls[:600]
        out = OUT / "calibrate.jsonl"
    else:
        urls = urls[: a.limit] if a.limit else urls
        out = OUT / "products.jsonl"

    store = Store(slug="avellaneda", name="Avellaneda a un Toque",
                  domain="www.avellanedaauntoque.com", platform="avellaneda")

    async with Fetcher(delay=0.0) as f:
        adapter = AvellanedaAdapter(store, f)
        crawler = BulkCrawler(
            adapter, out,
            start_concurrency=a.start_concurrency,
            max_concurrency=a.max_concurrency,
        )
        stats = await crawler.run(urls, progress_every=100 if a.calibrate else 1000)

    print("\n=== techo alcanzado ===")
    for k, v in stats.items():
        print(f"  {k:14} {v}")
    if crawler.errors:
        print(f"\n  errores ({len(crawler.errors)}), primeros 5:")
        for u, e in crawler.errors[:5]:
            print(f"    {e:34} {u[-46:]}")


asyncio.run(main())
