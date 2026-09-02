"""Detección por ENDPOINT en vez de por firma en el HTML.

Sniffear el HTML falla cuando la home es un cascarón, está detrás de un
CDN o el tema no menciona la plataforma. Pero la API o no está, o
responde: pedirle directamente `/products.json` a un dominio es una
prueba mucho más dura que buscar la palabra "shopify" en el markup.

Cuesta 5 requests por marca y no tiene falsos positivos.
"""
import asyncio
import json
import re
import sys

sys.path.insert(0, "src")
import httpx

from fadia.fetch import UA

MARCAS = [
    "revolver.com.ar", "kevingston.com", "etiqueta-negra.com", "grimoldi.com.ar",
    "vitamina.com", "vitamina.com.ar", "wanama.com", "tucciweb.com",
    "yagmour.com.ar", "markova.com", "muaa.com.ar", "isadora.com",
    "todomoda.com", "vanesakrongold.com", "gusman.com", "sweetvictorian.com.ar",
    "ginebra.com", "paulacahendanvers.com.ar", "sofimartire.com.ar",
    "hushpuppies.com.ar", "newbalance.com.ar", "puma.com.ar", "vans.com.ar",
    "lee.com.ar", "febo.com", "evangelinabomparola.com", "kosiuko.com.ar",
    "jazminchebar.com.ar", "bolivia.com.ar", "uma.com.ar", "ossira.com.ar",
    "akiabara.com", "awada.com.ar", "prototype.com.ar", "activity.com.ar",
]

_CUENTA = re.compile(r'"account"\s*:\s*"([a-z0-9][a-z0-9-]{1,22})"', re.I)


async def json_ok(client, url: str, sem, metodo="GET", **kw):
    async with sem:
        try:
            r = await (client.post(url, **kw) if metodo == "POST"
                       else client.get(url, **kw))
        except Exception:                                # noqa: BLE001
            return None
    if r.status_code not in (200, 206):
        return None
    if "json" not in r.headers.get("content-type", ""):
        return None
    try:
        return r.json()
    except json.JSONDecodeError:
        return None


async def sondear(client, dom: str, sem) -> dict:
    f = {"dominio": dom, "plataforma": "", "senal": "", "n": 0}

    # --- Shopify: /products.json
    d = await json_ok(client, f"https://{dom}/products.json?limit=2", sem, timeout=25.0)
    if isinstance(d, dict) and isinstance(d.get("products"), list):
        f.update(plataforma="shopify", senal="/products.json",
                 n=len(d["products"]))
        return f

    # --- VTEX: dominio público y, si no, el interno de la cuenta
    for host in (dom,):
        d = await json_ok(
            client, f"https://{host}/api/catalog_system/pub/products/search?_from=0&_to=1",
            sem, timeout=25.0)
        if isinstance(d, list) and d:
            f.update(plataforma="vtex", senal="catalog_system", n=len(d))
            return f
    async with sem:
        try:
            home = (await client.get(f"https://{dom}/", timeout=25.0)).text
        except Exception:                                # noqa: BLE001
            home = ""
    for cuenta in (list(dict.fromkeys(_CUENTA.findall(home)))[:3] if home else []):
        d = await json_ok(
            client,
            f"https://{cuenta}.vtexcommercestable.com.br/api/catalog_system/pub/products/search?_from=0&_to=1",
            sem, timeout=25.0)
        if isinstance(d, list) and d:
            f.update(plataforma="vtex", senal=f"{cuenta}.vtexcommercestable", n=len(d))
            return f

    # --- Magento: GraphQL
    d = await json_ok(
        client, f"https://{dom}/graphql", sem, metodo="POST", timeout=30.0,
        json={"query": "{products(search:\"\" pageSize:2 currentPage:1){total_count items{sku name}}}"},
        headers={"Content-Type": "application/json"})
    if isinstance(d, dict) and (d.get("data") or {}).get("products"):
        f.update(plataforma="magento", senal="/graphql",
                 n=(d["data"]["products"] or {}).get("total_count") or 0)
        return f

    # --- Tiendanube: sitemap + data-variants en una ficha
    async with sem:
        try:
            r = await client.get(f"https://{dom}/sitemap.xml", timeout=25.0)
            if r.status_code == 200 and "/productos/" in r.text:
                f.update(plataforma="tiendanube", senal="sitemap /productos/",
                         n=r.text.count("/productos/"))
                return f
        except Exception:                                # noqa: BLE001
            pass

    f["plataforma"] = "—"
    return f


async def main() -> None:
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(headers={"User-Agent": UA},
                                 follow_redirects=True) as c:
        filas = await asyncio.gather(*(sondear(c, d, sem) for d in MARCAS))

    import collections
    hallados = [f for f in filas if f["plataforma"] != "—"]
    for f in sorted(filas, key=lambda x: (x["plataforma"] == "—", x["plataforma"])):
        print(f"  {f['dominio'][:26]:28} {f['plataforma'][:11]:12} "
              f"{f['senal'][:34]:36} {f['n'] or ''}")
    print(f"\n  detectadas por endpoint: {len(hallados)}/{len(filas)}")
    for p, n in collections.Counter(f["plataforma"] for f in hallados).most_common():
        print(f"    {p:12} {n}")

    import csv
    from pathlib import Path
    out = Path("data/endpoints_ok.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["marca", "dominio"])
        w.writeheader()
        w.writerows({"marca": f["dominio"].split(".")[0], "dominio": f["dominio"]}
                    for f in hallados)
    print(f"\n  -> {out}")


asyncio.run(main())
