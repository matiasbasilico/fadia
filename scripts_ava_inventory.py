"""Inventario COMPLETO de avellanedaauntoque.com desde el índice de sitemaps.

139.044 publicaciones enumeradas con 16 requests, no 139.044: la URL
`/p/{id}-{slug}` ya trae identidad y nombre. La descarga de cada ficha
solo hace falta para el enriquecimiento (precio, local, imágenes).
"""
import asyncio
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")
from fadia.adapters.avellaneda import AvellanedaAdapter
from fadia.fetch import Fetcher
from fadia.models import Store
from fadia.normalize.taxonomy import classify_category, classify_gender

OUT = Path("data/avellaneda")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    store = Store(slug="avellaneda", name="Avellaneda a un Toque",
                  domain="www.avellanedaauntoque.com", platform="avellaneda")

    async with Fetcher(delay=0.5, concurrency=2) as f:
        ad = AvellanedaAdapter(store, f)
        cats = await ad.discover_categories()
        sellers = await ad.discover_sellers()
        entries = await ad.discover_with_lastmod()

    print(f"rubros   : {len(cats):>7}")
    print(f"locales  : {len(sellers):>7}")
    print(f"productos: {len(entries):>7}")

    rows, seen = [], set()
    for url, lastmod in entries:
        ident = AvellanedaAdapter.peek_from_url(url)
        if not ident or ident["external_id"] in seen:
            continue
        seen.add(ident["external_id"])
        title = ident["title_guess"]
        rows.append({
            "external_id": ident["external_id"],
            "title": title,
            "handle": ident["handle"],
            "category": classify_category(title, [], ident["handle"]) or "",
            "gender": classify_gender(title, []).value,
            "lastmod": lastmod or "",
            "url": url,
        })

    with (OUT / "inventory.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with (OUT / "categories.json").open("w", encoding="utf-8") as fh:
        json.dump({"rubros": cats, "locales": sellers}, fh, ensure_ascii=False, indent=1)

    print(f"\nunicos   : {len(rows):>7}  -> {OUT/'inventory.csv'}")
    print(f"duplicados en sitemap: {len(entries) - len(rows)}")

    cc = Counter(r["category"] or "(sin clasificar)" for r in rows)
    print(f"\ncategoria inferida del slug — cobertura "
          f"{100*(len(rows)-cc['(sin clasificar)'])/len(rows):.1f}%")
    for k, v in cc.most_common(18):
        print(f"    {k:20} {v:7}  {100*v/len(rows):5.1f}%")

    print("\nfechas de ultima modificacion (top 6):")
    for k, v in Counter(r["lastmod"][:10] for r in rows if r["lastmod"]).most_common(6):
        print(f"    {k}  {v:7}")


asyncio.run(main())
