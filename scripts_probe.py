"""Banco de pruebas: ¿cuánto generaliza el adapter de Tiendanube?

Corre el mismo parser contra tiendas que nunca vio y mide cobertura
campo por campo. Es la única forma honesta de contestar
"¿va a andar en todas?".
"""
import asyncio
import sys

sys.path.insert(0, "src")
from fadia.adapters.tiendanube import TiendanubeAdapter
from fadia.detect import detect_platform
from fadia.fetch import Fetcher
from fadia.models import Store

DOMAINS = [
    "alabamatienda.com.ar", "ankarabsas.com.ar", "ar.ilovea2.com",
    "lepouaccesorios.com.ar", "companiadesombreros.com.ar",
    "guardabosques.mitiendanube.com", "celsiusbackpacks.mitiendanube.com",
    "minianima.com.ar", "rainbowcompania.com", "philomenahome.com.ar",
]
N = 6


async def probe(domain: str, fetcher: Fetcher) -> dict:
    row = {"dominio": domain, "plataforma": "?", "fichas": 0, "ok": 0,
           "variantes": 0, "talle": 0, "color": 0, "precio": 0, "img": 0,
           "cat": 0, "desc": 0, "error": ""}
    try:
        home = await fetcher.get(f"https://{domain}/")
        row["plataforma"] = detect_platform(home)
        if row["plataforma"] != "tiendanube":
            return row
        store = Store(slug=domain.split(".")[0], name=domain,
                      domain=domain, platform="tiendanube")
        ad = TiendanubeAdapter(store, fetcher)
        urls = await ad.discover_product_urls()
        row["fichas"] = len(urls)
        for u in urls[:N]:
            try:
                p = await ad.parse_product(u)
            except Exception as e:
                row["error"] = row["error"] or f"{type(e).__name__}: {str(e)[:55]}"
                continue
            if not p:
                row["error"] = row["error"] or "parse devolvio None"
                continue
            row["ok"] += 1
            row["variantes"] += len(p.variants)
            row["talle"] += sum(1 for v in p.variants if v.size)
            row["color"] += sum(1 for v in p.variants if v.color)
            row["precio"] += sum(1 for v in p.variants if v.price.amount_cents > 0)
            row["img"] += len(p.images)
            row["cat"] += 1 if p.category else 0
            row["desc"] += 1 if p.description else 0
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {str(e)[:55]}"
    return row


async def main() -> None:
    async with Fetcher(delay=1.0, concurrency=3) as f:
        rows = await asyncio.gather(*(probe(d, f) for d in DOMAINS))

    hdr = (f"{'dominio':32} {'plataforma':11} {'fichas':>6} {'ok':>3} {'var':>4} "
           f"{'talle':>5} {'color':>5} {'$ok':>4} {'img':>4} {'cat':>4} {'desc':>4}  error")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['dominio'][:32]:32} {r['plataforma'][:11]:11} {r['fichas']:6} "
              f"{r['ok']:3} {r['variantes']:4} {r['talle']:5} {r['color']:5} "
              f"{r['precio']:4} {r['img']:4} {r['cat']:4} {r['desc']:4}  {r['error']}")


asyncio.run(main())
