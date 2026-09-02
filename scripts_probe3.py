"""Prueba los tres adapters nuevos contra tiendas reales de shopping."""
import asyncio
import sys

sys.path.insert(0, "src")
from fadia.adapters.magento import MagentoAdapter
from fadia.adapters.shopify import ShopifyAdapter
from fadia.adapters.vtex import VtexAdapter
from fadia.fetch import Fetcher
from fadia.models import Availability, Store

N = 25
TIENDAS = [
    (ShopifyAdapter, "aynotdead.com"), (ShopifyAdapter, "eyelit.com.ar"),
    (ShopifyAdapter, "crocs.com.ar"),
    (VtexAdapter, "47street.com.ar"), (VtexAdapter, "atomik.com.ar"),
    (VtexAdapter, "dash.com.ar"),
    (MagentoAdapter, "carocuore.com.ar"), (MagentoAdapter, "babycottons.com.ar"),
    (MagentoAdapter, "bowen.com.ar"),
]


async def probar(cls, domain, fetcher):
    slug = domain.split(".")[0]
    store = Store(slug=slug, name=slug, domain=domain, platform=cls.platform)
    ad = cls(store, fetcher)
    fila = {"dom": domain, "plat": cls.platform, "n": 0, "var": 0, "talle": 0,
            "color": 0, "precio": 0, "desc": 0, "img": 0, "cat": 0, "stock": 0,
            "err": ""}
    try:
        prods = []
        async for p in ad.harvest(limit=N):
            prods.append(p)
        fila["n"] = len(prods)
        for p in prods:
            fila["var"] += len(p.variants)
            fila["talle"] += sum(1 for v in p.variants if v.size)
            fila["color"] += sum(1 for v in p.variants if v.color)
            fila["precio"] += sum(1 for v in p.variants if v.price.amount_cents > 0)
            fila["stock"] += sum(1 for v in p.variants if v.stock is not None)
            fila["desc"] += 1 if p.description else 0
            fila["img"] += len(p.images)
            fila["cat"] += 1 if p.category else 0
        fila["_prods"] = prods
    except Exception as e:                               # noqa: BLE001
        fila["err"] = f"{type(e).__name__}: {str(e)[:60]}"
    return fila


async def main():
    async with Fetcher(delay=0.4, concurrency=4) as f:
        filas = await asyncio.gather(*(probar(c, d, f) for c, d in TIENDAS))

    hdr = (f"{'tienda':22} {'plat':10} {'prod':>4} {'var':>4} {'talle':>5} "
           f"{'color':>5} {'$':>4} {'stock':>5} {'img':>4} {'cat':>4} {'desc':>4}  error")
    print(hdr); print("-" * len(hdr))
    for r in filas:
        print(f"{r['dom'][:22]:22} {r['plat']:10} {r['n']:4} {r['var']:4} {r['talle']:5} "
              f"{r['color']:5} {r['precio']:4} {r['stock']:5} {r['img']:4} {r['cat']:4} "
              f"{r['desc']:4}  {r['err']}")

    print("\n=== muestras ===")
    for r in filas:
        for p in (r.get("_prods") or [])[:1]:
            v = p.variants[0]
            print(f"\n  {r['plat']} · {p.title[:44]}")
            print(f"    ${p.price_min.amount_cents/100:,.0f}"
                  + (f" (antes ${p.price_min.compare_at_cents/100:,.0f}, "
                     f"-{p.price_min.discount_pct:.0f}%)" if p.price_min.compare_at_cents else "")
                  + f" · {p.category} · {len(p.variants)} variantes · {len(p.images)} fotos")
            print(f"    v0: talle={v.size.normalized if v.size else None!r} "
                  f"color={v.color.raw if v.color else None!r} "
                  f"stock={v.stock} disp={v.availability.value}")
            print(f"    talles disponibles: {p.sizes_available[:8]}")


asyncio.run(main())
