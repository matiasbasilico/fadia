"""¿Qué exponen las marcas que ningún adapter de plataforma cubre?

Antes de escribir el extractor genérico hay que saber contra qué se escribe:
qué fracción tiene sitemap, cuántas publican JSON-LD de producto, y si ese
JSON-LD trae precio o solo el nombre.
"""
import asyncio
import json
import re
import sys

sys.path.insert(0, "src")
import httpx

from fadia.fetch import UA

_LDJSON = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_PROD_HINT = re.compile(r"/(producto|productos|product|products|p|item|shop)/", re.I)

MARCAS = [
    "revolver.com.ar", "kevingston.com", "etiqueta-negra.com", "grimoldi.com.ar",
    "vitamina.com", "wanama.com", "tucciweb.com", "yagmour.com.ar",
    "markova.com", "muaa.com.ar", "isadora.com", "todomoda.com",
    "vanesakrongold.com", "gusman.com", "sweetvictorian.com.ar", "ginebra.com",
    "paulacahendanvers.com.ar", "sofimartire.com.ar", "hushpuppies.com.ar",
    "newbalance.com.ar", "puma.com.ar", "vans.com.ar", "lee.com.ar",
    "febo.com", "evangelinabomparola.com",
]


def tipos_ld(html: str) -> list[str]:
    out = []
    for b in _LDJSON.findall(html):
        try:
            d = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        for x in (d if isinstance(d, list) else [d]):
            if isinstance(x, dict):
                t = x.get("@type")
                out += t if isinstance(t, list) else [str(t)]
                for g in (x.get("@graph") or []):
                    if isinstance(g, dict):
                        out.append(str(g.get("@type")))
    return out


async def sondear(client, dom: str, sem) -> dict:
    f = {"dominio": dom, "home": "", "sitemap": 0, "urls_prod": 0,
         "ld_home": "", "ld_ficha": "", "precio": "", "nota": ""}
    async with sem:
        try:
            r = await client.get(f"https://{dom}/", timeout=25.0)
            f["home"] = str(r.status_code)
            html = r.text
        except Exception as e:                           # noqa: BLE001
            f["nota"] = type(e).__name__
            return f
    f["ld_home"] = ",".join(sorted(set(tipos_ld(html)))[:3]) or "—"

    # sitemap
    urls: list[str] = []
    for sm in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
        async with sem:
            try:
                r = await client.get(f"https://{dom}{sm}", timeout=25.0)
            except Exception:                            # noqa: BLE001
                continue
        if r.status_code != 200 or "<loc>" not in r.text:
            continue
        locs = _LOC.findall(r.text)
        f["sitemap"] = len(locs)
        hijos = [u for u in locs if u.endswith(".xml")][:3]
        # No filtrar por patrón de path: muchas tiendas usan URLs planas
        # (/mi-vestido) y el filtro las descartaba, dando falsos "sin datos".
        urls = [u for u in locs if not u.endswith(".xml")]
        for h in hijos:
            async with sem:
                try:
                    rh = await client.get(h, timeout=25.0)
                except Exception:                        # noqa: BLE001
                    continue
            urls += [u for u in _LOC.findall(rh.text) if not u.endswith(".xml")]
            if len(urls) > 20:
                break
        break
    f["urls_prod"] = len(urls)

    if not urls:
        # último recurso: un link de producto desde el home
        m = re.findall(r'href=["\'](https?://[^"\']*?/(?:producto|productos|product|products|p)/[^"\']+)', html)
        urls = m[:1]

    # probar hasta 5 URLs: la primera del sitemap suele ser el home o una
    # página institucional, no una ficha de producto
    import random
    random.seed(3)
    muestra = urls[:2] + random.sample(urls, min(3, len(urls))) if urls else []
    for u in muestra:
        async with sem:
            try:
                rp = await client.get(u, timeout=25.0)
                tipos = tipos_ld(rp.text)
                if "Product" not in tipos:
                    # señales alternativas: OpenGraph de producto o microdata
                    if 'og:type" content="product' in rp.text or "itemtype" in rp.text and "schema.org/Product" in rp.text:
                        f["ld_ficha"] = "og/microdata"
                    continue
                f["ld_ficha"] = ",".join(sorted(set(tipos))[:3]) or "—"
                if "Product" in tipos:
                    for b in _LDJSON.findall(rp.text):
                        try:
                            d = json.loads(b.strip())
                        except json.JSONDecodeError:
                            continue
                        for x in (d if isinstance(d, list) else [d]):
                            if isinstance(x, dict) and "Product" in str(x.get("@type")):
                                of = x.get("offers") or {}
                                of = of[0] if isinstance(of, list) and of else of
                                f["precio"] = str((of or {}).get("price") or "")[:12]
                if f["precio"]:
                    break
            except Exception as e:                       # noqa: BLE001
                f["nota"] = type(e).__name__
    return f


async def main() -> None:
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers={"User-Agent": UA},
                                 follow_redirects=True) as c:
        filas = await asyncio.gather(*(sondear(c, d, sem) for d in MARCAS))

    hdr = (f"{'dominio':26} {'home':5} {'sitemap':>8} {'urls':>6} "
           f"{'ld home':22} {'ld ficha':22} {'precio':>10}")
    print(hdr); print("-" * len(hdr))
    for f in filas:
        print(f"{f['dominio'][:26]:26} {f['home']:5} {f['sitemap']:8} {f['urls_prod']:6} "
              f"{f['ld_home'][:22]:22} {f['ld_ficha'][:22]:22} {f['precio']:>10} {f['nota']}")

    con_ld = [f for f in filas if "Product" in f["ld_ficha"]]
    con_precio = [f for f in con_ld if f["precio"]]
    con_sitemap = [f for f in filas if f["sitemap"]]
    print(f"\n  sitemap          : {len(con_sitemap)}/{len(filas)}")
    print(f"  JSON-LD Product  : {len(con_ld)}/{len(filas)}")
    print(f"  con precio       : {len(con_precio)}/{len(filas)}")


asyncio.run(main())
