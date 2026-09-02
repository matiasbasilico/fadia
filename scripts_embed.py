"""Backfill de embeddings contra LM Studio.

El texto que se embebe no es solo el título: se arma un resumen con los
atributos que la gente usa para buscar (categoría, colores, talles, local).
Un título como "MINI SPARK" no dice nada solo; con "pollera, negro, talles
1 2 3, local X" empieza a ser encontrable por "pollera negra corta".
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")
from fadiaapi.llm import LMStudio
from fadia.storage.repository import Repository

DSN = "postgresql://fadia:fadia@localhost:5433/fadia"


def texto(r: dict) -> str:
    partes = [r["title"]]
    if r.get("category"):
        partes.append(r["category"])
    if r.get("brand"):
        partes.append(r["brand"])
    if r.get("seller_name"):
        partes.append(r["seller_name"])
    if r.get("colors_available"):
        partes.append("colores " + " ".join(r["colors_available"][:6]))
    if r.get("sizes_available"):
        partes.append("talles " + " ".join(r["sizes_available"][:8]))
    if d := r.get("description"):
        partes.append(d[:280])
    return " | ".join(p for p in partes if p)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--limit", type=int, default=None, help="tope de productos")
    a = ap.parse_args()

    repo = Repository(a.dsn)
    llm = LMStudio()
    hechos = 0
    t0 = time.monotonic()
    try:
        while True:
            rows = repo.products_without_embedding(limit=a.batch)
            if not rows:
                break
            # El servidor del modelo corta la conexión cada tantos miles de
            # pedidos. Sin reintento, el backfill muere a mitad y hay que
            # relanzarlo a mano; con reintento, se recupera solo.
            vecs = None
            for intento in range(4):
                try:
                    vecs = await llm.embed([texto(r) for r in rows])
                    break
                except Exception as e:                       # noqa: BLE001
                    espera = 2 ** intento
                    print(f"    reintento {intento+1}/4 tras {type(e).__name__} "
                          f"(espero {espera}s)", flush=True)
                    await asyncio.sleep(espera)
            if vecs is None:
                print("    lote fallido tras 4 intentos, sigo con el próximo", flush=True)
                continue
            hechos += repo.set_embeddings(zip((r["product_uid"] for r in rows), vecs))
            rate = hechos / max(time.monotonic() - t0, 1e-9)
            print(f"  {hechos:>7} embebidos  {rate:5.1f}/s", flush=True)
            if a.limit and hechos >= a.limit:
                break
    finally:
        await llm.aclose()
        repo.close()
    print(f"[listo] {hechos} embeddings en {time.monotonic()-t0:.0f}s")


asyncio.run(main())
