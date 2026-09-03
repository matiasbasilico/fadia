"""Le pone descripción, a partir de la FOTO, a los productos que no la tienen.

43.383 productos (29 % del catálogo) no publican descripción. Su embedding
queda armado casi solo con el título, y para "Body Microtul" o "2 en 1 Dama"
eso no alcanza para encontrarlos. Los 43.383 SÍ tienen foto: es la única
señal real que existe para ellos.

Gemma 4 es multimodal, así que se le pide que describa la prenda y ese texto
entra al embedding. No hay espacio vectorial nuevo ni fusión de puntajes: el
texto generado se embebe con el mismo modelo que todo lo demás.

    uv run python scripts_describir.py                # todo lo que falte
    uv run python scripts_describir.py --limite 200   # una tanda de prueba
    uv run python scripts_describir.py --hilos 6

Es REANUDABLE: se guarda de a lotes y solo toma los que siguen sin
description_ia, así que se puede cortar con Ctrl-C y volver a arrancar.

Medido en una MacBook Air M2: ~2.970 prendas/hora con 6 hilos. Los 43.383
son unas 14,6 horas. Más hilos no mejora — LM Studio serializa la inferencia
y a partir de 6 solo se agrega espera.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import time
import urllib.error
import urllib.request

import psycopg

DSN = os.getenv("DSN", "postgresql://fadia:fadia@localhost:5433/fadia")
LMS = os.getenv("LMSTUDIO_URL", "http://localhost:1234/v1")
MODELO = os.getenv("CHAT_MODEL", "google/gemma-4-e4b")

# Gemma describe en español peninsular si no se le pide otra cosa: en la
# primera prueba dijo "sudadera" por "buzo". Como el texto va a alimentar
# búsquedas escritas por argentinos, el vocabulario importa tanto como la
# descripción.
# La foto de catálogo casi siempre muestra un CONJUNTO armado. Sin anclar
# el modelo en qué se vende, describe la prenda más llamativa: para
# "FALDA BRAND NITE" —foto de remera a rayas + minifalda— devolvió "buzo a
# rayas". En un índice de búsqueda eso es peor que no tener descripción,
# porque contradice al título en vez de completarlo.
PROMPT = """Sos parte de un buscador de moda argentino.

EL PRODUCTO QUE SE VENDE ES: {que}
La foto puede mostrar un conjunto completo con otras prendas puestas.
Describí ÚNICAMENTE el producto que se vende. Ignorá el resto del outfit,
el fondo y la persona.

Usá vocabulario rioplatense, NO de España:
buzo (no sudadera), remera (no camiseta), campera (no chaqueta),
pollera (no falda), zapatillas (no deportivas), calza (no mallas),
corpiño (no sujetador), bombacha (no braga).

Nombrá en una frase: tipo de prenda, color, tela aparente, silueta y un
detalle distintivo. Sin marcas, sin precios, sin opinar, sin mencionar la
foto ni lo que ves. Máximo 25 palabras.

Si lo que se vende NO es una prenda ni un accesorio de vestir (una gift
card, por ejemplo), respondé exactamente: NO_ES_PRENDA"""

# El texto es para embeber, no para leer: si el modelo se va por las ramas
# ensucia el vector en vez de mejorarlo.
MAX_CARACTERES = 320
TIEMPO_IMAGEN = 45
TIEMPO_MODELO = 300
LOTE = 40           # cada cuántas filas se persiste


def pendientes(conn, limite: int | None) -> list[tuple]:
    sql = """
        SELECT product_uid, title, images->0->>'url', category
        FROM product
        WHERE (description IS NULL OR length(trim(description)) < 20)
          AND description_ia IS NULL
          AND jsonb_array_length(images) > 0
        ORDER BY store_slug <> 'avellaneda' DESC, product_uid
    """
    if limite:
        sql += f" LIMIT {int(limite)}"
    return conn.execute(sql).fetchall()


def _pedir(url: str, datos: bytes | None = None,
           cabeceras: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=datos, headers=cabeceras or {})
    return urllib.request.urlopen(req, timeout=timeout).read()


def describir(fila: tuple) -> tuple[str, str | None, str]:
    """Devuelve (uid, descripción, motivo). Si falla, descripción es None."""
    uid, title, img, categoria = fila
    if not img:
        return uid, None, "sin imagen"
    try:
        crudo = _pedir(img, cabeceras={"User-Agent": "Mozilla/5.0"},
                       timeout=TIEMPO_IMAGEN)
    except Exception as e:                              # noqa: BLE001
        return uid, None, f"imagen: {type(e).__name__}"

    cuerpo = {
        "model": MODELO, "max_tokens": 120, "reasoning_effort": "none",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT.format(
                que=f"{title}" + (f" (categoría: {categoria})" if categoria else ""))},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(crudo).decode()}},
        ]}],
    }
    try:
        r = json.loads(_pedir(f"{LMS}/chat/completions",
                              datos=json.dumps(cuerpo).encode(),
                              cabeceras={"Content-Type": "application/json"},
                              timeout=TIEMPO_MODELO))
        txt = (r["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:                              # noqa: BLE001
        return uid, None, f"modelo: {type(e).__name__}"

    txt = " ".join(txt.split())[:MAX_CARACTERES]
    if "NO_ES_PRENDA" in txt.upper():
        return uid, None, "no es prenda"
    # El modelo a veces comenta la foto en vez de describir el producto.
    # Ese texto en el vector hace que la prenda aparezca al buscar la
    # muletilla, no la prenda.
    bajo = txt.lower()
    if any(m in bajo for m in ("no es una prenda", "no describe", "esta imagen",
                               "la imagen", "no hay prenda", "gift card",
                               "tarjeta regalo", "no se puede")):
        return uid, None, "comentario, no descripción"
    # Una respuesta de tres palabras no aporta nada al vector.
    if len(txt) < 25:
        return uid, None, "respuesta corta"
    return uid, txt, "ok"


def guardar(conn, lote: list[tuple[str, str]]) -> None:
    if not lote:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE product SET description_ia = %s, description_ia_at = now() "
            "WHERE product_uid = %s",
            [(txt, uid) for uid, txt in lote])
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hilos", type=int, default=6,
                    help="6 es el óptimo medido; más solo agrega espera")
    ap.add_argument("--limite", type=int, default=None)
    a = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        filas = pendientes(conn, a.limite)
        total = len(filas)
        if not total:
            print("  no queda nada pendiente")
            return
        print(f"  {total:,} productos sin descripción · {a.hilos} hilos")

        hechos = fallidos = 0
        motivos: dict[str, int] = {}
        pendiente: list[tuple[str, str]] = []
        t0 = time.monotonic()

        with cf.ThreadPoolExecutor(max_workers=a.hilos) as ex:
            try:
                for uid, txt, motivo in ex.map(describir, filas):
                    if txt:
                        pendiente.append((uid, txt))
                        hechos += 1
                    else:
                        fallidos += 1
                        motivos[motivo] = motivos.get(motivo, 0) + 1
                    if len(pendiente) >= LOTE:
                        guardar(conn, pendiente)
                        pendiente = []
                    n = hechos + fallidos
                    if n % LOTE == 0:
                        seg = time.monotonic() - t0
                        ritmo = n / seg * 3600
                        falta = (total - n) / max(ritmo, 1)
                        print(f"    {n:>6,}/{total:,}  {ritmo:>5,.0f}/h  "
                              f"faltan {falta:.1f} h  fallidos {fallidos}", flush=True)
            except KeyboardInterrupt:
                print("\n  cortado: se guarda lo que hay y se puede retomar")

        guardar(conn, pendiente)

    seg = time.monotonic() - t0
    print(f"\n  {hechos:,} descritos, {fallidos:,} fallidos en {seg/60:.1f} min")
    for m, n in sorted(motivos.items(), key=lambda kv: -kv[1]):
        print(f"    {m:24} {n:>6,}")
    if hechos:
        print("\n  siguiente paso:  uv run python scripts_embed.py --solo-ia")


main()
