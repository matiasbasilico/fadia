"""Resuelve el dominio real de una marca y detecta su plataforma.

Adivinar `marca.com.ar` falla más de lo que acierta: Tucci vende en
tucciweb.com, Zara en zara.com/ar, y varias usan `tienda`/`shop` de
prefijo. Se prueban varios candidatos por marca y se queda con el primero
que responda y tenga plataforma conocida — o, si ninguno la tiene, con el
primero que al menos exista.

    uv run python scripts_resolver.py --marcas data/marcas.txt
    uv run python scripts_resolver.py "Tucci" "Vitamina" "Awada"
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, "src")
import httpx

from fadia.detect import detect_platform
from fadia.fetch import UA
from fadia.normalize.text import slugify

SALIDA = Path("data/marcas_resueltas.csv")

# plantillas de dominio, en orden de probabilidad
PLANTILLAS = (
    "{b}.com.ar", "www.{b}.com.ar", "{b}.com", "www.{b}.com",
    "{b}web.com", "tienda{b}.com.ar", "shop{b}.com.ar", "{b}.ar",
)
CONOCIDAS = {"tiendanube", "shopify", "vtex", "magento", "zara", "avellaneda"}


def candidatos(marca: str, extra: str | None = None) -> list[str]:
    if extra:
        return [extra]
    b = slugify(marca).replace("-", "")
    b2 = slugify(marca)                       # con guiones: maria-cher
    doms = [p.format(b=b) for p in PLANTILLAS]
    if b2 != b:
        doms += [p.format(b=b2) for p in PLANTILLAS[:4]]
    return list(dict.fromkeys(doms))


async def probar(client, dom: str, sem: asyncio.Semaphore) -> tuple[str, str] | None:
    async with sem:
        try:
            r = await client.get(f"https://{dom}/", timeout=20.0)
        except Exception:                                # noqa: BLE001
            return None
    if r.status_code >= 400 or len(r.text) < 1200:
        # el host puede existir aunque rechace al bot
        p = detect_platform("", dom)
        return (dom, p) if p in CONOCIDAS else None
    return dom, detect_platform(r.text, dom)


async def resolver(client, marca: str, extra: str | None, sem) -> dict:
    resultados = await asyncio.gather(
        *(probar(client, d, sem) for d in candidatos(marca, extra)))
    vivos = [r for r in resultados if r]
    if not vivos:
        return {"marca": marca, "dominio": "", "plataforma": "", "estado": "no resuelve"}
    # preferir el que tenga plataforma que sabemos leer
    conocido = next((v for v in vivos if v[1] in CONOCIDAS), None)
    dom, plat = conocido or vivos[0]
    return {"marca": marca, "dominio": dom, "plataforma": plat,
            "estado": "ok" if plat in CONOCIDAS else "plataforma no cubierta"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("marcas", nargs="*")
    ap.add_argument("--archivo", type=Path, default=None,
                    help="txt con una marca por línea (o 'Marca=dominio')")
    ap.add_argument("--salida", type=Path, default=SALIDA)
    a = ap.parse_args()

    objetivos: list[tuple[str, str | None]] = [(m, None) for m in a.marcas]
    if a.archivo:
        for linea in a.archivo.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            if "=" in linea:
                m, d = linea.split("=", 1)
                objetivos.append((m.strip(), d.strip()))
            else:
                objetivos.append((linea, None))

    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(headers={"User-Agent": UA},
                                 follow_redirects=True) as client:
        filas = await asyncio.gather(
            *(resolver(client, m, d, sem) for m, d in objetivos))

    a.salida.parent.mkdir(parents=True, exist_ok=True)
    with a.salida.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["marca", "dominio", "plataforma", "estado"])
        w.writeheader(); w.writerows(filas)

    import collections
    ok = [f for f in filas if f["estado"] == "ok"]
    for f in sorted(filas, key=lambda x: (x["estado"] != "ok", x["marca"])):
        print(f"  {f['marca'][:22]:24} {f['dominio'][:28]:30} "
              f"{f['plataforma'][:11]:12} {f['estado']}")
    print(f"\n  {len(ok)}/{len(filas)} con plataforma cubierta")
    for p, n in collections.Counter(f["plataforma"] for f in ok).most_common():
        print(f"    {p:12} {n}")
    print(f"\n  -> {a.salida}")


asyncio.run(main())
