"""Directorios de locales de shoppings argentinos.

Varios shoppings del grupo IRSA corren WordPress y dejan la API REST
abierta con un custom post type de locales. Eso da el padrón de marcas por
shopping —con local, teléfono e Instagram— sin scrapear una sola página.

Lo que ESTO da es el padrón de marcas y su presencia física.
Lo que NO da es catálogo: para precios y stock hay que ir al e-commerce de
cada marca, que es lo que ya hacen los adapters de plataforma.
"""
import asyncio
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")
import httpx

from fadia.fetch import UA
from fadia.normalize.text import clean

OUT = Path("data/malls")

# shopping -> (dominio, post_type). Descubierto probando /wp-json/wp/v2/types
MALLS = [
    ("Abasto Shopping",      "www.abasto-shopping.com.ar",  "locales"),
    ("Alcorta Shopping",     "www.alcortashopping.com.ar",  "locales"),
    ("Alto Rosario",         "www.alto-rosario.com.ar",     "locales"),
    ("Alto Avellaneda",      "www.altoavellaneda.com.ar",   "store"),
]

# Los shoppings NO clasifican por producto sino por PÚBLICO ("Mujer",
# "Hombre", "Bebés y Niños"). Filtrar por palabras como "indumentaria"
# devuelve 95 de 602; hay que invertirlo y excluir lo que seguro no es ropa.
NO_ROPA = re.compile(
    r"gastronom|food|dining|servicio|service|est[eé]tica|beauty|farmac|"
    r"tecno|electro|electronic|entreten|entertainment|cine|banco|"
    r"supermerc|juguete|librer|home|deco|mascota|[óo]ptica|salud",
    re.I,
)
SI_ROPA = re.compile(
    r"mujer|hombre|women|men|calzado|footwear|accesorio|accessor|"
    r"deporte|sport|beb[eé]s|babies|kids|ni[nñ]os|indumentaria|ropa|moda",
    re.I,
)


def es_ropa(rubro: str) -> bool:
    if not rubro:
        return False
    if NO_ROPA.search(rubro):
        return False
    return bool(SI_ROPA.search(rubro))


async def taxonomia(client, domain: str, post_type: str) -> dict[int, str]:
    """id de término -> nombre, para poder clasificar por rubro."""
    out: dict[int, str] = {}
    try:
        r = await client.get(f"https://{domain}/wp-json/wp/v2/types/{post_type}")
        taxes = r.json().get("taxonomies", []) if r.status_code == 200 else []
    except Exception:                                    # noqa: BLE001
        taxes = []
    for tax in taxes:
        try:
            r = await client.get(f"https://{domain}/wp-json/wp/v2/{tax}",
                                 params={"per_page": 100})
            if r.status_code == 200:
                for t in r.json():
                    out[t["id"]] = clean(t.get("name", ""))
        except Exception:                                # noqa: BLE001
            continue
    return out


async def locales(client, mall: str, domain: str, post_type: str) -> list[dict]:
    terms = await taxonomia(client, domain, post_type)
    filas, page = [], 1
    while True:
        r = await client.get(f"https://{domain}/wp-json/wp/v2/{post_type}",
                             params={"per_page": 100, "page": page})
        if r.status_code != 200:
            break
        lote = r.json()
        if not lote:
            break
        for x in lote:
            acf = x.get("acf") or {}
            rubros = []
            for k, v in x.items():
                if k.startswith("categoria") or k in ("rubro", "rubros"):
                    if isinstance(v, list):
                        rubros += [terms.get(i, str(i)) for i in v]
            filas.append({
                "shopping": mall,
                "dominio": domain,
                "nombre": clean(re.sub(r"<[^>]+>", "", x["title"]["rendered"])),
                "slug": x.get("slug", ""),
                "url": x.get("link", ""),
                "rubro": " / ".join(dict.fromkeys(r for r in rubros if r)),
                "ubicacion": clean(str(acf.get("campo_ubicacion") or acf.get("ubicacion") or "")),
                "telefono": clean(str(acf.get("campo_telefono") or acf.get("telefono") or "")),
                "web": (acf.get("campo_url1") or acf.get("web") or ""),
            })
        if len(lote) < 100:
            break
        page += 1
    return filas


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=40.0,
                                 follow_redirects=True) as client:
        lotes = await asyncio.gather(
            *(locales(client, m, d, t) for m, d, t in MALLS),
            return_exceptions=True)

    filas = []
    for (mall, _, _), res in zip(MALLS, lotes):
        if isinstance(res, Exception):
            print(f"  [fail] {mall}: {type(res).__name__}")
            continue
        print(f"  {mall:22} {len(res):>4} locales")
        filas += res

    ropa = [f for f in filas if es_ropa(f["rubro"])]
    sin_rubro = [f for f in filas if not f["rubro"]]
    with (OUT / "locales.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(filas[0].keys()))
        w.writeheader(); w.writerows(filas)

    marcas = sorted({f["nombre"] for f in ropa})
    (OUT / "marcas_ropa.json").write_text(
        json.dumps(marcas, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n  total locales      : {len(filas)}")
    print(f"  de ropa/calzado    : {len(ropa)}")
    print(f"  marcas únicas      : {len(marcas)}")
    print(f"  con Instagram/web  : {sum(1 for f in ropa if f['web'])}")
    print(f"  con ubicación      : {sum(1 for f in ropa if f['ubicacion'])}")
    print(f"  sin rubro (a revisar): {len(sin_rubro)}")
    print(f"\n  -> {OUT/'locales.csv'} · {OUT/'marcas_ropa.json'}")


asyncio.run(main())
