"""Crawler de volumen: reanudable, con caudal adaptativo y telemetría.

Pensado para las 139.044 fichas de Avellaneda, donde tres cosas dejan de
ser opcionales:

- **Reanudable.** Un crawl de horas se corta. El progreso va a un archivo
  de checkpoint y al reiniciar se saltan las URLs ya hechas.
- **Streaming a disco.** 139k productos no entran cómodos en memoria; cada
  resultado se escribe apenas sale.
- **Sin cache en disco.** 139k páginas de ~60 KB son ~8 GB de HTML que no
  vamos a releer. Se guarda el JSON-LD ya parseado, que es lo que sirve
  para re-normalizar sin volver a scrapear.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Iterable

import httpx

from .adaptive import STRESS_STATUS, RateController
from .adapters.base import StoreAdapter
from .fetch import UA


class BulkCrawler:
    def __init__(
        self,
        adapter: StoreAdapter,
        out_path: Path,
        *,
        start_concurrency: int = 4,
        max_concurrency: int = 32,
        checkpoint_every: int = 250,
    ) -> None:
        self.adapter = adapter
        self.out_path = Path(out_path)
        self.state_path = self.out_path.with_suffix(".state.json")
        self.rate = RateController(concurrency=start_concurrency,
                                   max_concurrency=max_concurrency)
        self.checkpoint_every = checkpoint_every
        self.done: set[str] = set()
        self.errors: list[tuple[str, str]] = []
        self._started = time.monotonic()

    # ------------------------------------------------------------------ estado
    def _load_state(self) -> None:
        if self.out_path.exists():
            with self.out_path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        self.done.add(json.loads(line)["url"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        if self.state_path.exists():
            try:
                self.done.update(json.loads(self.state_path.read_text())["skipped"])
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_state(self, skipped: Iterable[str]) -> None:
        self.state_path.write_text(json.dumps({
            "skipped": sorted(skipped),
            "stats": self.rate.snapshot(),
            "errors": self.errors[-50:],
        }), encoding="utf-8")

    # ------------------------------------------------------------------ crawl
    async def run(self, urls: list[str], *, progress_every: int = 500) -> dict:
        self._load_state()
        pending = [u for u in urls if u not in self.done]
        skipped: set[str] = set()

        print(f"[crawl] {len(urls)} totales | {len(self.done)} ya hechas | "
              f"{len(pending)} pendientes", flush=True)
        if not pending:
            return self.rate.snapshot()

        client = httpx.AsyncClient(
            # Accept-Encoding lo pone httpx solo, con los codecs que REALMENTE
            # sabe decodificar. Forzarlo a mano fue un error caro: pedir 'br'
            # sin brotli instalado devuelve los bytes comprimidos como texto,
            # sin excepción y con HTTP 200. El crawl informó "600/600, 0
            # errores" y no extrajo un solo producto.
            headers={"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"},
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=self.rate.max_concurrency + 8,
                                max_keepalive_connections=self.rate.max_concurrency),
        )
        lock = asyncio.Lock()
        counter = {"n": 0}

        with self.out_path.open("a", encoding="utf-8") as sink:
            async def worker(url: str) -> None:
                await self.rate.acquire()
                t0 = time.monotonic()
                try:
                    resp = await client.get(url)
                    latency = time.monotonic() - t0

                    if resp.status_code in STRESS_STATUS:
                        ra = resp.headers.get("retry-after")
                        await self.rate.on_stress(float(ra) if ra and ra.isdigit() else None)
                        return                      # queda pendiente para el reintento
                    if resp.status_code >= 400:
                        await self.rate.on_failure()
                        async with lock:
                            skipped.add(url)
                            self.errors.append((url, f"HTTP {resp.status_code}"))
                        return

                    await self.rate.on_success(latency)
                    product = await self.adapter.parse_html(url, resp.text)
                    async with lock:
                        if product is None:
                            skipped.add(url)
                            self.errors.append((url, "sin datos de producto"))
                        else:
                            sink.write(product.model_dump_json() + "\n")
                        counter["n"] += 1
                        n = counter["n"]
                    if n % progress_every == 0:
                        sink.flush()
                        self._report(n, len(pending))
                    if n % self.checkpoint_every == 0:
                        self._save_state(skipped)

                except (httpx.TimeoutException, httpx.TransportError) as e:
                    await self.rate.on_stress()
                    async with lock:
                        self.errors.append((url, type(e).__name__))
                except Exception as e:            # noqa: BLE001 - no matar el crawl
                    await self.rate.on_failure()
                    async with lock:
                        skipped.add(url)
                        self.errors.append((url, f"{type(e).__name__}: {str(e)[:80]}"))
                finally:
                    self.rate.release()

            # cola acotada: no crear 139k tareas de una
            queue = asyncio.Queue()
            for u in pending:
                queue.put_nowait(u)

            async def pump() -> None:
                while True:
                    try:
                        url = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await worker(url)

            await asyncio.gather(*(pump() for _ in range(self.rate.max_concurrency)))
            sink.flush()

        await client.aclose()
        self._save_state(skipped)
        self._report(counter["n"], len(pending), final=True)
        return self.rate.snapshot()

    def _report(self, n: int, total: int, *, final: bool = False) -> None:
        s = self.rate.snapshot()
        elapsed = time.monotonic() - self._started
        rps = n / elapsed if elapsed else 0
        eta = (total - n) / rps / 60 if rps else 0
        tag = "[fin]  " if final else "[crawl]"
        print(f"{tag} {n}/{total} | {rps:5.1f} req/s | conc={s['concurrency']:2} "
              f"(pico {s['peak']:2}) | p50={s['p50_ms']:4}ms | 429/5xx={s['stressed']} "
              f"| err={s['failed']} | ETA {eta:.0f} min", flush=True)
