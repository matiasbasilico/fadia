"""Enriquecimiento: baja N fichas del inventario y mide qué campos rinden."""
import asyncio
import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, "src")
from fadia.adapters.avellaneda import AvellanedaAdapter
from fadia.fetch import Fetcher
from fadia.models import Store

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
OUT = Path("data/avellaneda")


async def main() -> None:
    rows = list(csv.DictReader((OUT / "inventory.csv").open(encoding="utf-8")))
    random.seed(7)
    sample = random.sample(rows, N)

    store = Store(slug="avellaneda", name="Avellaneda a un Toque",
                  domain="www.avellanedaauntoque.com", platform="avellaneda")

    async with Fetcher(delay=0.6, concurrency=4) as f:
        ad = AvellanedaAdapter(store, f)
        res = await asyncio.gather(*(ad.parse_product(r["url"]) for r in sample),
                                   return_exceptions=True)

    ok, fail = [], 0
    for r, p in zip(sample, res):
        if isinstance(p, Exception):
            print(f"  [fail] {r['external_id']}: {type(p).__name__}: {str(p)[:70]}")
            fail += 1
        elif p is None:
            print(f"  [none] {r['external_id']} {r['url']}")
            fail += 1
        else:
            ok.append(p)

    with (OUT / "sample.jsonl").open("w", encoding="utf-8") as fh:
        for p in ok:
            fh.write(p.model_dump_json(exclude={"raw"}) + "\n")

    n = len(ok) or 1
    print(f"\n=== {len(ok)}/{N} fichas parseadas, {fail} fallidas ===")
    for label, hits in [
        ("titulo",            sum(1 for p in ok if p.title)),
        ("precio mayorista",  sum(1 for p in ok if p.price_min.amount_cents > 0)),
        ("precio por menor",  sum(1 for p in ok if p.price_retail)),
        ("compra minima",     sum(1 for p in ok if p.min_purchase)),
        ("local vendedor",    sum(1 for p in ok if p.seller)),
        ("direccion local",   sum(1 for p in ok if p.seller and p.seller.address)),
        ("local verificado",  sum(1 for p in ok if p.seller and p.seller.verified)),
        ("seguidores",        sum(1 for p in ok if p.seller and p.seller.followers)),
        ("descripcion",       sum(1 for p in ok if p.description)),
        ("imagenes >=1",      sum(1 for p in ok if p.images)),
        ("categoria",         sum(1 for p in ok if p.category)),
        ("disponibilidad",    sum(1 for p in ok if p.availability.value != "unknown")),
    ]:
        bar = "█" * round(20 * hits / n)
        print(f"  {label:18} {hits:3}/{len(ok):<3} {100*hits/n:5.1f}%  {bar}")

    print("\n=== muestra ===")
    for p in ok[:6]:
        may = p.price_min.amount_cents / 100
        men = p.price_retail.amount_cents / 100 if p.price_retail else None
        print(f"  {p.title[:34]:36} may ${may:>9,.0f}"
              f"{'  men $' + format(men, ',.0f') if men else '':>18}"
              f"  min:{(p.min_purchase or '-')[:12]:14} {p.seller.name[:20] if p.seller else '-'}")


asyncio.run(main())
