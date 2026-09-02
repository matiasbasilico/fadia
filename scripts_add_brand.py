"""Sumar una marca al catálogo, de punta a punta.

    uv run python scripts_add_brand.py rapsodia.com.ar --nombre "Rapsodia"
    uv run python scripts_add_brand.py --lote data/malls/marcas_ecommerce.csv

Hace todo el recorrido: detecta la plataforma, cosecha con el adapter que
corresponda, pasa el arnés de validación y recién entonces ingiere y
embebe. Si el arnés bloquea, no escribe nada.

Es el camino por el que entra cualquier marca nueva: no hay que tocar
código salvo que la plataforma no esté cubierta.
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, "src")
import httpx

from fadia.adapters.avellaneda import AvellanedaAdapter
from fadia.adapters.magento import MagentoAdapter
from fadia.adapters.shopify import ShopifyAdapter
from fadia.adapters.tiendanube import TiendanubeAdapter
from fadia.adapters.vtex import VtexAdapter
from fadia.adapters.generico import GenericoAdapter
from fadia.adapters.zara import ZaraAdapter
from fadia.detect import detect_by_endpoint, detect_platform
from fadia.fetch import UA, Fetcher
from fadia.models import Store
from fadia.normalize.text import slugify
from fadia.storage.repository import Repository
from fadia.validate import Severity, validate

ADAPTERS = {
    "tiendanube": TiendanubeAdapter, "shopify": ShopifyAdapter,
    "vtex": VtexAdapter, "magento": MagentoAdapter,
    "avellaneda": AvellanedaAdapter, "zara": ZaraAdapter,
    "generico": GenericoAdapter,
}
DSN = "postgresql://fadia:fadia@localhost:5433/fadia"


async def detectar(domain: str) -> str:
    """Host -> HTML -> endpoints. En ese orden, de lo barato a lo caro."""
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30.0,
                                 follow_redirects=True) as c:
        if (p := detect_platform("", domain)) != "unknown":
            return p
        try:
            r = await c.get(f"https://{domain}/")
            html = r.text if r.status_code < 400 else ""
        except httpx.HTTPError as e:
            html, r = "", None
            if not html:
                p = await detect_by_endpoint(domain, c)
                return p if p != "unknown" else f"error:{type(e).__name__}"
        if (p := detect_platform(html, domain)) != "unknown":
            return p
        # el HTML no alcanzó: preguntarle a las APIs
        return await detect_by_endpoint(domain, c)


def slug_de(domain: str) -> str:
    """`www.zara.com` -> `zara`, no `www`.

    Partir por el primer punto sin sacar el `www.` produce el slug `www`
    para todo dominio con prefijo, y todas esas marcas terminan pisándose
    dentro de la misma tienda.
    """
    host = domain.lower().strip().removeprefix("https://").removeprefix("http://")
    host = host.split("/")[0].removeprefix("www.")
    return slugify(host.split(".")[0])


async def sumar(domain: str, nombre: str | None, limite: int | None,
                repo: Repository, fetcher: Fetcher) -> dict:
    slug = slug_de(domain)
    fila = {"marca": nombre or slug, "dominio": domain, "plataforma": "",
            "productos": 0, "variantes": 0, "estado": ""}

    plataforma = await detectar(domain)
    fila["plataforma"] = plataforma
    if plataforma not in ADAPTERS:
        # Escalón 2: sin adapter de plataforma, se intenta leer los datos
        # estructurados que el sitio publica para los buscadores.
        if plataforma.startswith("error:"):
            fila["estado"] = "no responde"
            return fila
        plataforma = "generico"
        fila["plataforma"] = "generico"

    store = Store(slug=slug, name=nombre or slug.title(),
                  domain=domain, platform=plataforma)
    adapter = ADAPTERS[plataforma](store, fetcher)

    productos = []
    try:
        async for p in adapter.harvest(limit=limite):
            productos.append(p)
    except Exception as e:                               # noqa: BLE001
        fila["estado"] = f"cosecha falló: {type(e).__name__}"
        return fila
    if not productos:
        fila["estado"] = "0 productos"
        return fila

    hallazgos = validate(productos)
    bloqueantes = [f for f in hallazgos if f.severity is Severity.BLOCK]
    if bloqueantes:
        fila["estado"] = "ARNÉS BLOQUEÓ: " + bloqueantes[0].check
        return fila

    repo.upsert_store(store)
    conteos = repo.upsert_products(productos)
    fila.update(productos=conteos["productos"], variantes=conteos["variantes"],
                estado="ok" + (f" · {len(hallazgos)} avisos" if hallazgos else ""))
    return fila


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dominios", nargs="*", help="dominios a sumar")
    ap.add_argument("--nombre", default=None)
    ap.add_argument("--lote", type=Path, default=None,
                    help="CSV con columnas marca,dominio")
    ap.add_argument("--limite", type=int, default=None, help="tope de productos por marca")
    ap.add_argument("--dsn", default=DSN)
    a = ap.parse_args()

    objetivos: list[tuple[str, str | None]] = [(d, a.nombre) for d in a.dominios]
    if a.lote:
        for r in csv.DictReader(a.lote.open(encoding="utf-8")):
            if r.get("dominio"):
                objetivos.append((r["dominio"], r.get("marca")))
    # dedup por dominio
    vistos, unicos = set(), []
    for d, n in objetivos:
        if d not in vistos:
            vistos.add(d); unicos.append((d, n))

    repo = Repository(a.dsn)
    filas = []
    try:
        async with Fetcher(delay=0.5, concurrency=4) as f:
            for d, n in unicos:
                fila = await sumar(d, n, a.limite, repo, f)
                filas.append(fila)
                print(f"  {fila['marca'][:22]:24} {fila['dominio'][:26]:28} "
                      f"{fila['plataforma'][:11]:12} {fila['productos']:5} prod  "
                      f"{fila['estado']}", flush=True)
    finally:
        repo.close()

    ok = [f for f in filas if f["estado"].startswith("ok")]
    print(f"\n  {len(ok)}/{len(filas)} marcas sumadas · "
          f"{sum(f['productos'] for f in ok)} productos · "
          f"{sum(f['variantes'] for f in ok)} variantes")
    sin = [f for f in filas if f["estado"] == "sin adapter"]
    if sin:
        print(f"  sin adapter ({len(sin)}): "
              + ", ".join(f"{f['marca']} [{f['plataforma']}]" for f in sin[:8]))
    print("\n  después: uv run python scripts_embed.py")


asyncio.run(main())
