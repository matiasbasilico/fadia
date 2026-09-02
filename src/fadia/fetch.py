"""Cliente HTTP: rate limit por dominio + cache en disco.

La cache no es una optimización: durante el desarrollo del parser vas a
releer la misma página cincuenta veces y no corresponde pegarle cincuenta
veces al server de la tienda.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_DELAY = 1.0          # segundos entre requests al mismo dominio
DEFAULT_TIMEOUT = 30.0


class Fetcher:
    def __init__(
        self,
        cache_dir: Path | str = "data/cache",
        delay: float = DEFAULT_DELAY,
        concurrency: int = 4,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self._sem = asyncio.Semaphore(concurrency)
        self._last_hit: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:20]}.html"

    async def get(self, url: str, *, use_cache: bool = True) -> str:
        cached = self._cache_path(url)
        if use_cache and cached.exists():
            return cached.read_text(encoding="utf-8")

        async with self._sem:
            host = httpx.URL(url).host or ""
            elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            resp = await self._client.get(url)
            self._last_hit[host] = time.monotonic()

        resp.raise_for_status()
        cached.write_text(resp.text, encoding="utf-8")
        return resp.text

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
