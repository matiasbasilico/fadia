"""API del buscador conversacional.

El flujo del chat, en orden:

  pregunta -> filtros por reglas (precio, talle, categoría, color)
           -> embedding de la pregunta
           -> SQL: filtros duros + orden por distancia coseno
           -> Gemma 4 redacta SOBRE las filas devueltas, en streaming

El modelo nunca elige productos ni inventa precios: recibe un contexto
numerado y solo puede citar de ahí. Es la diferencia entre un buscador y
una máquina de alucinar stock.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from .llm import CHAT_MODEL, EMBED_MODEL, LMStudio
from .search import (Filters, SearchService, facetas, parse_filters,
                     texto_de_busqueda)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://fadia:fadia@localhost:5433/fadia")

SYSTEM = """Sos el asistente de FadIA, un buscador de moda argentina.

TONO — editorial, como una nota de pasarela o una descripción de colección:
- Registro neutro y preciso. Nada de muletillas ni coloquialismos:
  prohibido "che", "te tiro", "copado", "onda", "mirá que", "buenísimo".
- Hablá de la prenda: silueta, caída, textura, paleta, ocasión. El
  vocabulario del rubro, no el de la charla de café.
- Frases cortas y afirmativas. Ni entusiasmo forzado ni signos de
  exclamación.
- Tratá al lector de "vos" sin caer en la jerga porteña.

Ejemplo del registro buscado:
  "Cuatro vestidos negros de línea sobria, entre el satén y el morley.
   El [2] destaca por su caída y el [5] por el trabajo de escote.
   ¿Preferís una silueta entallada o más recta?"

REGLAS QUE NO PODÉS ROMPER:
- Respondé SOLO con los productos del contexto. Si está vacío, decilo.
- Nunca inventes precios, talles, stock ni locales. Si un dato no está,
  aclarás que no está.
- Citá los productos por su número: [1], [2].
- Los precios son en pesos argentinos. En los mayoristas hay precio por
  mayor y compra mínima ("curva"): si preguntan cuánto sale, aclaralo.
- Es una CONVERSACIÓN: "y en negro?" o "más barato" se refieren a lo que
  venías mostrando. Usá el historial.
- No repitas el resumen de lo anterior: respondé lo nuevo.

ESTRUCTURA:
- Una frase que caracterice el CONJUNTO: qué son y qué tienen en común.
- Después, 1 o 2 piezas que se destaquen, con el motivo.
- Cierre: UNA pregunta breve que acote, tomada de "PARA REPREGUNTAR".
  Nunca repitas un eje ya preguntado en el hilo.
- Máximo 4 líneas.
- Si preguntan un dato puntual, respondé ESO y nada más: sin
  recomendaciones que nadie pidió y sin pregunta de cierre.
"""

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8,
                          kwargs={"row_factory": dict_row}, open=True)
    state["pool"] = pool
    state["search"] = SearchService(pool)
    state["llm"] = LMStudio()
    yield
    await state["llm"].aclose()
    pool.close()


app = FastAPI(title="FadIA", lifespan=lifespan)


class Turno(BaseModel):
    """Un intercambio previo del hilo."""
    pregunta: str = Field(max_length=600)
    respuesta: str = Field(default="", max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=600)
    # Historial del hilo. Sin esto cada pregunta arranca de cero y
    # "¿y en negro?" pierde de qué se estaba hablando.
    historial: list[Turno] = Field(default_factory=list, max_length=6)
    limit: int = Field(default=8, ge=1, le=20)
    canal: str | None = Field(default=None, pattern="^(mayorista|marcas)$")
    genero: str | None = Field(default=None, pattern="^(women|men|kids|sin)$")


class SearchRequest(BaseModel):
    q: str = Field(default="", max_length=300)
    limit: int = Field(default=12, ge=1, le=48)
    canal: str | None = Field(default=None, pattern="^(mayorista|marcas)$")
    genero: str | None = Field(default=None, pattern="^(women|men|kids|sin)$")


@app.get("/health")
async def health() -> dict:
    llm = await state["llm"].health()
    try:
        with state["pool"].connection() as conn:
            conn.execute("SELECT 1")
        db = {"ok": True}
    except Exception as e:                          # noqa: BLE001
        db = {"ok": False, "error": str(e)}
    return {"db": db, "lmstudio": llm,
            "models": {"chat": CHAT_MODEL, "embed": EMBED_MODEL}}


@app.get("/stats")
async def stats() -> dict:
    return state["search"].stats()


@app.post("/search")
async def search(req: SearchRequest) -> dict:
    filters = parse_filters(req.q)
    if req.canal:
        filters.canal = req.canal
    if req.genero:
        filters.genero = req.genero
    emb = None
    if req.q.strip():
        try:
            emb = await state["llm"].embed_one(req.q)
        except Exception:                           # noqa: BLE001
            emb = None                              # sin LM Studio, cae a trigrama
    rows, aflojados = state["search"].search_relaxed(
        embedding=emb, filters=filters, text=req.q, limit=req.limit)
    return {"filtros": filters.applied, "aflojados": aflojados,
            "modo": "semantico" if emb else "texto", "resultados": rows}


def _context(rows: list[dict]) -> str:
    """Contexto numerado. Solo hechos verificables de la base."""
    if not rows:
        return "(sin resultados)"
    out = []
    for i, r in enumerate(rows, 1):
        partes = [f"[{i}] {r['title']}", f"${r['price']:,.0f}"]
        if r.get("price_retail"):
            partes.append(f"(por menor ${r['price_retail']:,.0f})")
        if r.get("seller_name"):
            partes.append(f"local: {r['seller_name']}")
            if r.get("seller_address"):
                partes.append(f"({r['seller_address']})")
        elif r.get("brand"):
            partes.append(f"marca: {r['brand']}")
        if r.get("min_purchase"):
            partes.append(f"compra mínima: {r['min_purchase']}")
        if r.get("sizes_available"):
            partes.append(f"talles: {', '.join(r['sizes_available'][:8])}")
        if r.get("colors_available"):
            partes.append(f"colores: {', '.join(r['colors_available'][:6])}")
        out.append(" · ".join(partes))
    return "\n".join(out)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        preguntas = [t.pregunta for t in req.historial[-4:]]
        filters: Filters = parse_filters(req.message, preguntas)
        if req.canal:
            filters.canal = req.canal
        if req.genero:
            filters.genero = req.genero
        yield _sse("filters", {"applied": filters.applied, "canal": filters.canal,
                               "genero": filters.genero})

        # El vector se arma con el hilo, no solo con el último mensaje: la
        # ocasión ("para un casamiento") no es filtrable y vivía únicamente
        # acá, así que se perdía en cuanto el usuario escribía otra cosa.
        try:
            emb = await state["llm"].embed_one(
                texto_de_busqueda(req.message, preguntas))
        except Exception as e:                      # noqa: BLE001
            emb = None
            yield _sse("warn", {"message": f"sin embeddings ({type(e).__name__}), "
                                           f"busco por texto"})

        rows, aflojados = state["search"].search_relaxed(
            embedding=emb, filters=filters, text=req.message, limit=req.limit)

        # Ejes ya preguntados en el hilo: se saltean para que la repregunta
        # avance (silueta -> tela -> detalle) en vez de repetirse.
        usados = {e for t in req.historial for e in ("tela", "silueta", "detalle")
                  if e in (t.respuesta or "").lower()}
        ejes = facetas(rows, usados)

        yield _sse("results", {"resultados": rows, "aflojados": aflojados,
                               "modo": "semantico" if emb else "texto",
                               "facetas": ejes})

        extra_canal = {
            "mayorista": "\nEl usuario compra POR MAYOR para revender: la compra mínima "
                         "(la curva) es tan importante como el precio. Si te preguntan cuánto "
                         "sale, calculá precio x mínimo.",
            "marcas": "\nCompra para uso propio: hablá de talle, marca y ocasión, "
                      "no de curvas ni de reventa.",
        }.get(filters.canal or "", "")
        # El historial va como turnos reales, no aplastado dentro del
        # prompt: así el modelo distingue quién dijo qué. Las respuestas
        # anteriores se recortan porque lo que importa de ellas es el hilo,
        # no el detalle de cada producto — ese ya viene en el contexto nuevo.
        previos: list[dict] = []
        for t in req.historial[-4:]:
            previos.append({"role": "user", "content": t.pregunta[:400]})
            if t.respuesta:
                previos.append({"role": "assistant", "content": t.respuesta[:400]})

        messages = [
            {"role": "system", "content": SYSTEM + extra_canal},
            *previos,
            {"role": "user", "content":
                f"Productos disponibles:\n{_context(rows)}\n\n"
                + (f"NOTA: no hubo coincidencias exactas, se aflojaron estos "
                   f"filtros: {', '.join(aflojados)}. Avisale al usuario en una "
                   f"línea antes de recomendar.\n\n" if aflojados else "")
                + (f"PARA REPREGUNTAR (ejes disponibles en estos resultados): "
                   + " | ".join(f"{e['eje']}: {', '.join(e['opciones'])}" for e in ejes)
                   + "\n\n" if ejes else "")
                + f"Pregunta del usuario: {req.message}"},
        ]
        try:
            async for canal, texto in state["llm"].stream_chat(messages):
                yield _sse(canal, {"text": texto})
        except Exception as e:                      # noqa: BLE001
            yield _sse("error", {"message": f"{type(e).__name__}: {e}"})
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/product/{product_uid:path}")
async def product(product_uid: str) -> dict:
    with state["pool"].connection() as conn:
        row = conn.execute("SELECT * FROM product WHERE product_uid = %s",
                           (product_uid,)).fetchone()
        if not row:
            raise HTTPException(404, "no existe")
        row["variantes"] = conn.execute(
            "SELECT * FROM variant WHERE product_uid = %s ORDER BY external_id",
            (product_uid,)).fetchall()
        row["historico"] = conn.execute("""
            SELECT observed_at, price_cents, availability, stock
            FROM price_point WHERE product_uid = %s
            ORDER BY observed_at DESC LIMIT 60
        """, (product_uid,)).fetchall()
    row.pop("embedding", None)
    row.pop("raw", None)
    return row
