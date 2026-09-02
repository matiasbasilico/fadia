"""Control de caudal adaptativo (AIMD) para crawls largos.

El objetivo es "el máximo que el servidor acepta", que no es un número que
uno elija de antemano: depende de la hora, del plan de hosting y de lo que
esté haciendo el resto del mundo con ese sitio.

Estrategia, prestada de TCP: **subir de a poco, bajar de golpe.**
Mientras las respuestas llegan sanas y rápidas se agrega concurrencia de a
una; ante el primer 429, 5xx o timeout se corta la concurrencia a la mitad
y se respeta el `Retry-After`. Así el crawl converge solo al techo real
sin cruzarlo de forma sostenida.

Una avalancha a ciegas es peor para todos: el server se degrada, la IP
termina bloqueada y el crawl queda incompleto a mitad de camino.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# señales de que el servidor está sufriendo
STRESS_STATUS = {429, 500, 502, 503, 504, 408}

MIN_CONCURRENCY = 1
PROBE_WINDOW = 40          # respuestas sanas seguidas antes de subir un escalón
LATENCY_GUARD = 2.5        # si p50 se multiplica por esto vs. el mejor, frenar


@dataclass
class RateController:
    """Concurrencia variable + freno por latencia."""

    concurrency: int = 4
    max_concurrency: int = 32
    _sem: asyncio.Semaphore = field(init=False)
    _healthy_streak: int = 0
    _best_latency: float = field(default=float("inf"))
    _recent: list[float] = field(default_factory=list)
    _cooldown_until: float = 0.0
    _lock: asyncio.Lock = field(init=False)

    # telemetría
    ok: int = 0
    stressed: int = 0
    failed: int = 0
    backoffs: int = 0
    peak_concurrency: int = 0

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.concurrency)
        self._lock = asyncio.Lock()
        self.peak_concurrency = self.concurrency

    async def acquire(self) -> None:
        if (wait := self._cooldown_until - time.monotonic()) > 0:
            await asyncio.sleep(wait)
        await self._sem.acquire()

    def release(self) -> None:
        self._sem.release()

    async def on_success(self, latency: float) -> None:
        async with self._lock:
            self.ok += 1
            self._healthy_streak += 1
            self._recent.append(latency)
            if len(self._recent) > PROBE_WINDOW:
                self._recent.pop(0)
            self._best_latency = min(self._best_latency, latency)

            if self._healthy_streak < PROBE_WINDOW or self.concurrency >= self.max_concurrency:
                return

            # freno por latencia: el server puede no dar 429 y aun así ahogarse
            p50 = sorted(self._recent)[len(self._recent) // 2]
            if self._best_latency and p50 > self._best_latency * LATENCY_GUARD:
                self._healthy_streak = 0
                return

            self.concurrency += 1                      # additive increase
            self.peak_concurrency = max(self.peak_concurrency, self.concurrency)
            self._sem.release()
            self._healthy_streak = 0

    async def on_stress(self, retry_after: float | None = None) -> None:
        """429/5xx: cortar a la mitad y esperar."""
        async with self._lock:
            self.stressed += 1
            self.backoffs += 1
            self._healthy_streak = 0
            target = max(MIN_CONCURRENCY, self.concurrency // 2)   # multiplicative decrease
            for _ in range(self.concurrency - target):
                try:
                    await asyncio.wait_for(self._sem.acquire(), timeout=0.001)
                except (TimeoutError, asyncio.TimeoutError):
                    break
            self.concurrency = target
            self._cooldown_until = time.monotonic() + (retry_after or 5.0)

    async def on_failure(self) -> None:
        async with self._lock:
            self.failed += 1
            self._healthy_streak = 0

    def snapshot(self) -> dict[str, float | int]:
        p50 = sorted(self._recent)[len(self._recent) // 2] if self._recent else 0.0
        return {
            "concurrency": self.concurrency,
            "peak": self.peak_concurrency,
            "ok": self.ok,
            "stressed": self.stressed,
            "failed": self.failed,
            "backoffs": self.backoffs,
            "p50_ms": round(p50 * 1000),
            "best_ms": round(self._best_latency * 1000) if self._recent else 0,
        }
