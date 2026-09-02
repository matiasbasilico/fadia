"""¿Las marcas de shopping tienen e-commerce que ya sabemos scrapear?

Prueba dominios candidatos por marca y detecta la plataforma. Es la
pregunta que decide si el directorio de shoppings sirve como semilla:
un padrón de marcas sin catálogo no agrega precios ni stock.
"""
import asyncio
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
import httpx

from fadia.detect import detect_platform
from fadia.fetch import UA
from fadia.normalize.text import slugify

OUT = Path("data/malls")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 60
TLDS = (".com.ar", ".com", ".ar")


async def probar(client, marca: str, sem: asyncio.Semaphore) -> dict:
    base = slugify(marca).replace("-", "")
    fila = {"marca": marca, "dominio": "", "plataforma": "", "http": ""}
    for tld in TLDS:
        dom = f"{base}{tld}"
        async with sem:
            try:
                r = await client.get(f"https://{dom}/", timeout=18.0)
            except Exception:                            # noqa: BLE001
                continue
        if r.status_code >= 400 or len(r.text) < 2000:
            continue
        fila.update(dominio=dom, http=str(r.status_code),
                    plataforma=detect_platform(r.text))
        return fila
    return fila


async def main() -> None:
    marcas = json.loads((OUT / "marcas_ropa.json").read_text(encoding="utf-8"))
    # normalizar duplicados por mayúsculas (CHEEKY / Cheeky)
    vistas, unicas = set(), []
    for m in marcas:
        k = slugify(m)
        if k and k not in vistas:
            vistas.add(k); unicas.append(m)
    unicas = unicas[:LIMIT]

    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers={"User-Agent": UA},
                                 follow_redirects=True) as client:
        filas = await asyncio.gather(*(probar(client, m, sem) for m in unicas))

    con = [f for f in filas if f["dominio"]]
    conocidas = [f for f in con if f["plataforma"] != "unknown"]

    with (OUT / "marcas_ecommerce.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["marca", "dominio", "plataforma", "http"])
        w.writeheader(); w.writerows(filas)

    print(f"  probadas             : {len(unicas)}")
    print(f"  con sitio resuelto   : {len(con)}  ({100*len(con)/len(unicas):.0f}%)")
    print(f"  plataforma conocida  : {len(conocidas)}  ({100*len(conocidas)/len(unicas):.0f}%)")
    import collections
    for p, n in collections.Counter(f["plataforma"] for f in con).most_common():
        print(f"     {p:14} {n}")
    print("\n  ejemplos:")
    for f in conocidas[:14]:
        print(f"     {f['marca'][:24]:26} {f['dominio'][:30]:32} {f['plataforma']}")


asyncio.run(main())
