"""¿Sin razonamiento el modelo sigue siendo confiable?

La velocidad no sirve si el modelo empieza a inventar. Se comparan las dos
configuraciones sobre los mismos casos, incluido el que importa: contexto
que NO contiene lo que el usuario pide.
"""
import asyncio
import json
import time

import httpx

URL = "http://localhost:1234/v1/chat/completions"
MODELO = "google/gemma-4-e4b"

SYSTEM = """Sos el asistente de compras de FadIA, un buscador de indumentaria argentina.
- Respondé SOLO con los productos del contexto. Si el contexto no tiene lo pedido, decilo.
- Nunca inventes precios, talles, stock ni locales.
- Citá los productos por su número: [1], [2].
- Español rioplatense, breve. Recomendá 2 o 3 y explicá por qué."""

REMERAS = """[1] Remeras de Dama Talle Unico · $3.800 · local: Joaco Mayorista · compra mínima: 5 unidades
[2] Liquidacion Camiseta Morley Invierno · $2.500 · local: Stilo Arcana · compra mínima: 3 prendas
[3] Remera Princesa · $6.800 · local: Mixlatino Flama · compra mínima: 5"""

COSMETICOS = """[1] Fijador de Maquillaje de Tei · $2.200 · local: Moda Bonita · compra mínima: 6 artículos
[2] Delineador Cremoso Retractil · $600 · local: Moda Bonita · compra mínima: 6 artículos
[3] Polvo Traslúcido con Esponja · $3.800 · local: Moda Bonita · compra mínima: 6 artículos"""

CASOS = [
    ("recomendación normal", REMERAS,
     "quiero revender remeras por mayor, ¿qué local me conviene?"),
    ("cálculo con la curva", REMERAS,
     "si llevo el mínimo de [1], ¿cuánto gasto?"),
    ("TRAMPA: no hay lo pedido", COSMETICOS,
     "necesito jeans de hombre talle 42, ¿qué tenés?"),
    ("TRAMPA: dato ausente", REMERAS,
     "¿de qué color viene la Remera Princesa?"),
]


async def preguntar(client, contexto, pregunta, extra):
    payload = {"model": MODELO, "temperature": 0.3, "max_tokens": 1200,
               "messages": [
                   {"role": "system", "content": SYSTEM},
                   {"role": "user",
                    "content": f"Productos:\n{contexto}\n\nPregunta: {pregunta}"}],
               **extra}
    t0 = time.monotonic()
    r = await client.post(URL, json=payload)
    dt = time.monotonic() - t0
    d = r.json()
    if "choices" not in d:
        return dt, f"(sin respuesta) {json.dumps(d, ensure_ascii=False)[:200]}"
    m = d["choices"][0]["message"]
    return dt, (m.get("content") or "").strip()


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for nombre, ctx, preg in CASOS:
            print(f"\n{'='*76}\n{nombre}  —  «{preg}»\n{'='*76}")
            for etiqueta, extra in (("CON razonamiento", {}),
                                    ("SIN razonamiento", {"reasoning_effort": "none"})):
                dt, txt = await preguntar(client, ctx, preg, extra)
                print(f"\n  [{etiqueta}]  {dt:.1f}s")
                for linea in txt.split("\n")[:6]:
                    if linea.strip():
                        print(f"    {linea.strip()[:96]}")


asyncio.run(main())
