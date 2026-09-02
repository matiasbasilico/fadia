"""Latencia real de punta a punta, como la percibe el usuario.

Mide los tres hitos que importan en la pantalla:
  filtros  -> aparecen los chips
  grilla   -> aparecen las tarjetas (esto ya era rápido)
  1er texto-> empieza a escribirse la respuesta  <- lo que se sentía lento
"""
import asyncio
import json
import time

import httpx

API = "http://localhost:8080/chat"

PREGUNTAS = [
    "quiero revender remeras por mayor, ¿qué local me conviene?",
    "vestido negro talle 40 menos de 30 lucas",
    "camperas de abrigo hasta $25.000",
    "algo canchero para una fiesta de noche",
    "zapatillas talle 38",
]


async def medir(client, pregunta):
    hitos = {}
    t0 = time.monotonic()
    n_res = 0
    texto = ""
    ev = None
    async with client.stream("POST", API, json={"message": pregunta, "limit": 8}) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                t = time.monotonic() - t0
                if ev == "filters":
                    hitos.setdefault("filtros", t)
                elif ev == "results":
                    hitos.setdefault("grilla", t)
                    n_res = len(d["resultados"])
                elif ev == "answer":
                    hitos.setdefault("primer_texto", t)
                    texto += d["text"]
                elif ev == "done":
                    hitos["total"] = t
    return hitos, n_res, texto.strip()


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        print(f"{'pregunta':44} {'filtros':>8} {'grilla':>8} {'1er txt':>8} {'total':>8} {'n':>3}")
        print("-" * 86)
        totales = []
        for p in PREGUNTAS:
            try:
                h, n, txt = await medir(client, p)
            except Exception as e:                       # noqa: BLE001
                print(f"{p[:44]:44} ERROR {type(e).__name__}: {str(e)[:40]}")
                continue
            totales.append(h.get("total", 0))
            print(f"{p[:44]:44} {h.get('filtros',0)*1000:7.0f}ms "
                  f"{h.get('grilla',0)*1000:7.0f}ms {h.get('primer_texto',0):7.1f}s "
                  f"{h.get('total',0):7.1f}s {n:3}")
        if totales:
            print(f"\n  mediana total: {sorted(totales)[len(totales)//2]:.1f}s")

        print("\n=== ejemplo de respuesta ===")
        _, _, txt = await medir(client, PREGUNTAS[0])
        print(txt[:600])


asyncio.run(main())
