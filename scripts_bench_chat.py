"""Mide dónde se va el tiempo de una respuesta del chat.

Separa tres cosas que se confunden en "el chat es lento":
  - tiempo hasta el primer token (arranque)
  - tokens gastados RAZONANDO antes de escribir
  - tokens de la respuesta en sí
"""
import argparse
import asyncio
import json
import time

import httpx

URL = "http://localhost:1234/v1/chat/completions"

SYSTEM = """Sos el asistente de compras de FadIA, un buscador de indumentaria argentina.
- Respondé SOLO con los productos del contexto.
- Nunca inventes precios, talles, stock ni locales.
- Citá los productos por su número: [1], [2].
- Español rioplatense, breve. Recomendá 2 o 3 y explicá por qué."""

CONTEXTO = """[1] Remeras de Dama Talle Unico · $3.800 · local: Joaco Mayorista · compra mínima: 5 unidades
[2] Liquidacion Camiseta Morley Invierno · $2.500 · local: Stilo Arcana · compra mínima: 3 prendas
[3] Remera Princesa · $6.800 · local: Mixlatino Flama · compra mínima: 5
[4] Remera Angel · $6.500 · local: Mixlatino Flama · compra mínima: 3 unidades
[5] Remera Basica C/rulete X Talles · $4.500 · local: La Bella Mayorista · compra mínima: 3 remeras
[6] Remera Hombre · $5.500 · local: Sol Ángel Fit · compra mínima: 6"""

PREGUNTA = "quiero revender remeras por mayor, ¿qué local me conviene?"


async def corrida(client, modelo: str, etiqueta: str, extra: dict,
                  system: str = SYSTEM, contexto: str = CONTEXTO) -> dict:
    payload = {
        "model": modelo,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Productos:\n{contexto}\n\nPregunta: {PREGUNTA}"},
        ],
        "temperature": 0.3,
        "max_tokens": 1600,
        "stream": True,
        **extra,
    }
    t0 = time.monotonic()
    primer_razon = primer_texto = None
    razon = texto = ""
    try:
        async with client.stream("POST", URL, json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                body = line[6:].strip()
                if body == "[DONE]":
                    break
                try:
                    d = json.loads(body)["choices"][0]["delta"]
                except Exception:                        # noqa: BLE001
                    continue
                if t := d.get("reasoning_content"):
                    primer_razon = primer_razon or time.monotonic() - t0
                    razon += t
                if t := d.get("content"):
                    primer_texto = primer_texto or time.monotonic() - t0
                    texto += t
    except Exception as e:                               # noqa: BLE001
        return {"etiqueta": etiqueta, "error": f"{type(e).__name__}: {str(e)[:60]}"}

    total = time.monotonic() - t0
    return {
        "etiqueta": etiqueta,
        "total": total,
        "t_primer_texto": primer_texto or total,
        "chars_razon": len(razon),
        "chars_texto": len(texto),
        "muestra": texto.strip()[:150].replace("\n", " "),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeticiones", type=int, default=1)
    a = ap.parse_args()

    # variantes a comparar: (modelo, etiqueta, parámetros extra)
    PRUEBAS = [
        ("google/gemma-4-e4b", "gemma4 · actual", {}),
        ("google/gemma-4-e4b", "gemma4 · reasoning low",
         {"reasoning_effort": "low"}),
        ("google/gemma-4-e4b", "gemma4 · reasoning none",
         {"reasoning_effort": "none"}),
        ("google/gemma-4-e4b", "gemma4 · chat_template_kwargs",
         {"chat_template_kwargs": {"enable_thinking": False}}),
        ("google/gemma-4-e4b", "gemma4 · tope 300 tok",
         {"max_tokens": 300}),
    ]

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        filas = []
        for modelo, etiqueta, extra in PRUEBAS:
            mejores = []
            for _ in range(a.repeticiones):
                mejores.append(await corrida(client, modelo, etiqueta, extra))
            filas.append(min(
                (m for m in mejores if "error" not in m),
                key=lambda m: m["total"], default=mejores[0]))

    hdr = f"{'variante':32} {'total':>8} {'1er txt':>8} {'razona':>8} {'texto':>7}  respuesta"
    print(hdr); print("-" * 108)
    for f in filas:
        if "error" in f:
            print(f"{f['etiqueta']:32} {'—':>8} {'—':>8} {'—':>8} {'—':>7}  {f['error']}")
            continue
        print(f"{f['etiqueta']:32} {f['total']:7.1f}s {f['t_primer_texto']:7.1f}s "
              f"{f['chars_razon']:7}c {f['chars_texto']:6}c  {f['muestra'][:44]}")


asyncio.run(main())
