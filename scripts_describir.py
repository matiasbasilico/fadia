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
import random
import re
import io
import threading
import time
import urllib.error
import urllib.request

import psycopg
from PIL import Image

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

# La categoría ancla bien al modelo —evita que describa otra prenda del
# conjunto— pero es una etiqueta INTERNA en inglés, y el modelo la copiaba
# al texto: "Chaleco tipo knitwear beige". Pasaba en el 3,4 % de las
# descripciones. Se traduce antes de inyectarla.
CATEGORIA_ES = {
    "tops": "parte de arriba", "pants": "pantalón", "jeans": "jean",
    "shorts": "short", "skirts": "pollera", "dresses": "vestido",
    "knitwear": "prenda de punto", "outerwear": "abrigo",
    "lingerie": "lencería", "swimwear": "traje de baño",
    "activewear": "ropa deportiva", "shoes": "calzado", "bags": "bolso",
    "jewelry": "bijouterie", "accessories": "accesorio",
}

# El texto es para embeber, no para leer: si el modelo se va por las ramas
# ensucia el vector en vez de mejorarlo.
MAX_CARACTERES = 320
TIEMPO_IMAGEN = 45
TIEMPO_MODELO = 300
LOTE = 40           # cada cuántas filas se persiste

# ---------------------------------------------------------------- imagen
# LM Studio reparte el contexto (8192 tokens) entre sus 4 ranuras
# paralelas, así que con varias corridas a la vez una foto de 700 KB no
# entra en la fracción que le toca y el servidor responde 400. Secuencial
# andaba, en paralelo no: por eso el error aparecía solo en la corrida real.
#
# Achicar la foto lo resuelve y además la hace 97 % más liviana (492 KB ->
# 19 KB en la prueba). Para describir una prenda, 512 px sobran.
LADO_MAX = 512


def _achicar(crudo: bytes) -> bytes:
    """Reduce la foto a algo que entre cómodo en el contexto del modelo."""
    try:
        im = Image.open(io.BytesIO(crudo))
        im = im.convert("RGB")
        im.thumbnail((LADO_MAX, LADO_MAX))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        return buf.getvalue()
    except Exception:                                   # noqa: BLE001
        # Si el formato es raro, se manda como vino: peor es no describir.
        return crudo


# ---------------------------------------------------------------- cortesía
# Primera corrida: 6 hilos bajando imágenes sin ningún freno. El CDN de
# Avellaneda empezó a devolver errores de conexión y el script se comió
# 31.904 productos en minutos, marcándolos todos como fallidos — un fallo
# de red consumía el item en vez de reintentarlo.
#
# Dos correcciones:
#   1. Un freno por host, para no volver a golpear al mismo servidor.
#   2. Un error de red NO consume el producto: se espera y se reintenta.
#      Si el host sigue rechazando, se frena la corrida entera en vez de
#      quemar la cola.
PAUSA_POR_HOST = 0.12          # ~8 req/s repartidas entre los hilos
REINTENTOS_RED = 4
CORTE_POR_HOST = 60            # fallos seguidos de un host antes de abortar

# LM Studio sirve el modelo con 4 ranuras paralelas. Con 6 hilos mandando
# cargas de ~1 MB, las de más se rechazaban con HTTPError — y ese error
# también consumía el producto. Se limita la concurrencia contra el modelo
# y se reintenta igual que con las imágenes.
RANURAS_MODELO = 4          # las mismas que expone LM Studio
REINTENTOS_MODELO = 3
_ranuras = threading.Semaphore(RANURAS_MODELO)

_freno = threading.Lock()
_ultimo: dict[str, float] = {}
_seguidos: dict[str, int] = {}
_abortar = threading.Event()


def _esperar_turno(host: str) -> None:
    """Serializa los pedidos a un mismo host, sin frenar a los demás."""
    with _freno:
        ahora = time.monotonic()
        prox = _ultimo.get(host, 0) + PAUSA_POR_HOST
        espera = max(0.0, prox - ahora)
        _ultimo[host] = ahora + espera
    if espera:
        time.sleep(espera)


def _bajar_imagen(url: str) -> bytes:
    """Baja con freno y reintento. Un 403/429 es 'aflojá', no 'no existe'."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    ultimo_error: Exception | None = None
    for intento in range(REINTENTOS_RED):
        if _abortar.is_set():
            raise RuntimeError("corrida abortada")
        _esperar_turno(host)
        try:
            datos = _pedir(url, cabeceras={"User-Agent": "Mozilla/5.0"},
                           timeout=TIEMPO_IMAGEN)
            _seguidos[host] = 0
            return datos
        except Exception as e:                          # noqa: BLE001
            ultimo_error = e
            n = _seguidos.get(host, 0) + 1
            _seguidos[host] = n
            if n >= CORTE_POR_HOST:
                _abortar.set()
                raise RuntimeError(f"{host} rechaza {n} seguidos: se corta") from e
            # espera creciente con ruido, para no reintentar todos a la vez
            time.sleep(min(30, 1.5 * (2 ** intento)) * (0.7 + random.random() * 0.6))
    raise ultimo_error if ultimo_error else RuntimeError("sin datos")


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


def _que_se_vende(title: str, categoria: str | None) -> str:
    es = CATEGORIA_ES.get(categoria or "")
    return f"{title} (es un/a {es})" if es else title


def describir(fila: tuple) -> tuple[str, str | None, str]:
    """Devuelve (uid, descripción, motivo). Si falla, descripción es None."""
    uid, title, img, categoria = fila
    if not img:
        return uid, None, "sin imagen"
    try:
        crudo = _achicar(_bajar_imagen(img))
    except Exception as e:                              # noqa: BLE001
        detalle = getattr(e, "code", "") or type(e).__name__
        return uid, None, f"imagen: {detalle}"

    cuerpo = {
        "model": MODELO, "max_tokens": 120, "reasoning_effort": "none",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT.format(que=_que_se_vende(title, categoria))},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(crudo).decode()}},
        ]}],
    }
    txt = None
    ultimo: Exception | None = None
    for intento in range(REINTENTOS_MODELO):
        try:
            with _ranuras:
                r = json.loads(_pedir(f"{LMS}/chat/completions",
                                      datos=json.dumps(cuerpo).encode(),
                                      cabeceras={"Content-Type": "application/json"},
                                      timeout=TIEMPO_MODELO))
            txt = (r["choices"][0]["message"]["content"] or "").strip()
            break
        except Exception as e:                          # noqa: BLE001
            ultimo = e
            time.sleep(min(20, 2 * (2 ** intento)) * (0.7 + random.random() * 0.6))
    if txt is None:
        # El código HTTP importa: un 400 es la carga, un 429 es ritmo y un
        # 500 es el servidor. Sin el número, "HTTPError" no dice nada.
        detalle = getattr(ultimo, "code", "") or type(ultimo).__name__
        return uid, None, f"modelo: {detalle}"

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
    # Red de seguridad: si el modelo igual copia la etiqueta interna, se
    # saca del texto en vez de dejarla contaminando el vector.
    txt = re.sub(r"\s*\b(tipo\s+)?(knitwear|outerwear|activewear|swimwear|"
                 r"footwear|bottoms)\b", "", txt, flags=re.I).strip()
    txt = re.sub(r"\s{2,}", " ", txt)
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
