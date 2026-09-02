"""CLI: `uv run python -m fadia.cli <dominio> [--limit N]`"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from .adapters.avellaneda import AvellanedaAdapter
from .adapters.magento import MagentoAdapter
from .adapters.shopify import ShopifyAdapter
from .adapters.tiendanube import TiendanubeAdapter
from .adapters.vtex import VtexAdapter
from .adapters.generico import GenericoAdapter
from .adapters.zara import ZaraAdapter
from .detect import detect_platform
from .fetch import Fetcher
from .models import Store

ADAPTERS = {
    "tiendanube": TiendanubeAdapter,
    "avellaneda": AvellanedaAdapter,
    "shopify": ShopifyAdapter,
    "vtex": VtexAdapter,
    "magento": MagentoAdapter,
    "zara": ZaraAdapter,
    "generico": GenericoAdapter,
}


async def run(domain: str, limit: int | None, out: Path) -> int:
    domain = urlparse(domain if "://" in domain else f"https://{domain}").netloc
    slug = domain.replace("www.", "").split(".")[0]

    async with Fetcher() as fetcher:
        home = await fetcher.get(f"https://{domain}/")
        platform = detect_platform(home, domain)
        print(f"[detect] {domain} -> {platform}", file=sys.stderr)

        adapter_cls = ADAPTERS.get(platform)
        if not adapter_cls:
            print(f"[error] sin adapter para '{platform}'", file=sys.stderr)
            return 1

        store = Store(slug=slug, name=slug.upper(), domain=domain, platform=platform)
        adapter = adapter_cls(store, fetcher)

        products, errors = [], 0
        if platform in ("shopify", "vtex", "magento", "zara", "generico"):
            # adapters de API: cosechan JSON paginado, sin pasar por HTML
            async for p in adapter.harvest(limit=limit):
                products.append(p)
            print(f"[harvest] {len(products)} productos por API", file=sys.stderr)
        else:
            urls = await adapter.discover_product_urls()
            print(f"[discover] {len(urls)} fichas en el sitemap", file=sys.stderr)
            if limit:
                urls = urls[:limit]
            results = await asyncio.gather(
                *(adapter.parse_product(u) for u in urls), return_exceptions=True)
            for url, r in zip(urls, results):
                if isinstance(r, Exception):
                    print(f"[fail] {url}: {type(r).__name__}: {r}", file=sys.stderr)
                    errors += 1
                elif r is None:
                    print(f"[skip] {url}: sin datos", file=sys.stderr)
                    errors += 1
                else:
                    products.append(r)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for p in products:
            fh.write(p.model_dump_json(exclude={"raw"}) + "\n")

    print(f"[done] {len(products)} productos OK, {errors} con problema -> {out}",
          file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="fadia")
    ap.add_argument("domain")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", type=Path, default=Path("data/products.jsonl"))
    a = ap.parse_args()
    return asyncio.run(run(a.domain, a.limit, a.out))


if __name__ == "__main__":
    raise SystemExit(main())
