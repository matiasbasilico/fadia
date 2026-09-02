"""Contrato que cumple todo adapter.

Dos familias, un solo contrato:

- **HTML** (Tiendanube, Avellaneda): enumeran URLs y parsean páginas.
- **API** (Shopify, VTEX, Magento): piden JSON paginado y nunca tocan HTML.

`harvest()` es lo único que el resto del sistema necesita. Los adapters de
HTML lo heredan; los de API lo sobrescriben.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..fetch import Fetcher
from ..models import Product, Store


class StoreAdapter(ABC):
    platform: str

    def __init__(self, store: Store, fetcher: Fetcher) -> None:
        self.store = store
        self.fetcher = fetcher

    @abstractmethod
    async def discover_product_urls(self) -> list[str]:
        """Enumera todas las fichas de producto de la tienda."""

    async def parse_html(self, url: str, html: str) -> Product | None:
        """HTML ya descargado -> Product. Solo lo implementan los adapters de HTML.

        Separado de la descarga a propósito: el crawler de volumen maneja su
        propio cliente con caudal adaptativo y necesita parsear sin volver a
        pedir la página.
        """
        raise NotImplementedError(f"{type(self).__name__} es un adapter de API")

    async def parse_product(self, url: str) -> Product | None:
        """Camino corto: descarga y parsea. Para exploración y tests."""
        return await self.parse_html(url, await self.fetcher.get(url))

    async def harvest(self, limit: int | None = None) -> AsyncIterator[Product]:
        """Catálogo completo, producto a producto."""
        urls = await self.discover_product_urls()
        for i, url in enumerate(urls):
            if limit and i >= limit:
                return
            if (p := await self.parse_product(url)) is not None:
                yield p
