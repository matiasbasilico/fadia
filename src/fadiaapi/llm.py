"""Cliente de LM Studio (API compatible con OpenAI).

Gemma 4 es un modelo de razonamiento: en una prueba gastó 728 de 885 tokens
pensando antes de escribir una línea. Dos consecuencias de diseño:

- El presupuesto de tokens tiene que ser holgado o la respuesta sale vacía
  (con `max_tokens=120` devolvió string vacío y 117 tokens de razonamiento).
- Hay que **streamear**, o el usuario mira una pantalla quieta 30 segundos.
  El razonamiento viaja en `reasoning_content`, separado de `content`: se
  emite aparte para poder mostrarlo plegado en vez de mezclarlo con la
  respuesta.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx

LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://localhost:1234/v1")
CHAT_MODEL = os.getenv("CHAT_MODEL", "google/gemma-4-e4b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")

# holgado a propósito: ver docstring
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1600"))
# "none" apaga la cadena de razonamiento: 5x más rápido, misma fidelidad
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "none")
TIMEOUT = httpx.Timeout(600.0, connect=10.0)


class LMStudio:
    def __init__(self, base_url: str = LMSTUDIO_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        """Estado real de carga, no solo disponibilidad.

        `/v1/models` lista lo que existe; `/api/v0/models` dice cuál está
        **residente en memoria**, que es lo que decide la latencia: si el
        modelo de embeddings no está cargado, LM Studio lo carga en cada
        consulta y de paso desaloja al de chat. Medido, ese swap cuesta
        3,8 s por pregunta.
        """
        try:
            r = await self._client.get(f"{self.base_url}/models", timeout=8.0)
            r.raise_for_status()
            ids = [m["id"] for m in r.json().get("data", [])]
        except Exception as e:                      # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        cargados: set[str] = set()
        try:
            base_v0 = self.base_url.rsplit("/v1", 1)[0] + "/api/v0/models"
            r2 = await self._client.get(base_v0, timeout=8.0)
            if r2.status_code == 200:
                cargados = {m["id"] for m in r2.json().get("data", [])
                            if m.get("state") == "loaded"}
        except Exception:                           # noqa: BLE001
            pass

        avisos = []
        if cargados:
            for nombre, modelo in (("chat", CHAT_MODEL), ("embeddings", EMBED_MODEL)):
                if modelo not in cargados:
                    avisos.append(
                        f"el modelo de {nombre} ({modelo}) no está residente: "
                        f"cada consulta paga la recarga. Corré "
                        f"`lms load {modelo} --ttl 86400 -y`")
        return {"ok": True, "models": ids, "residentes": sorted(cargados),
                "chat_model_loaded": CHAT_MODEL in ids,
                "embed_model_loaded": EMBED_MODEL in ids,
                "reasoning_effort": REASONING_EFFORT or "(por defecto del modelo)",
                "avisos": avisos}

    # ------------------------------------------------------------------ embeddings
    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        r = await self._client.post(
            f"{self.base_url}/embeddings",
            json={"model": EMBED_MODEL, "input": texts})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    # ------------------------------------------------------------------ chat
    async def stream_chat(
        self, messages: list[dict], *, temperature: float = 0.3,
        max_tokens: int = MAX_TOKENS,
    ) -> AsyncIterator[tuple[str, str]]:
        """Emite `(canal, texto)` donde canal es 'reasoning' o 'answer'."""
        payload = {
            "model": CHAT_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if REASONING_EFFORT:
            payload["reasoning_effort"] = REASONING_EFFORT
        async with self._client.stream(
            "POST", f"{self.base_url}/chat/completions", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                body = line[6:].strip()
                if body == "[DONE]":
                    return
                try:
                    delta = json.loads(body)["choices"][0]["delta"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if txt := delta.get("reasoning_content"):
                    yield "reasoning", txt
                if txt := delta.get("content"):
                    yield "answer", txt
