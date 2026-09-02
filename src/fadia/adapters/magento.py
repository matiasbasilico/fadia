"""Adapter Magento 2 (GraphQL).

Magento es el más cerrado de los tres: no hay un `/products.json` ni una
API REST pública. Pero **`/graphql` sí está abierto** en las instalaciones
por defecto, y una sola query trae producto, variantes y precios.

Particularidades que cambian el mapeo:

- Los productos son `ConfigurableProduct` (con variantes) o `SimpleProduct`
  (sin ellas). Hay que pedir el fragmento `... on ConfigurableProduct` o las
  variantes no vienen.
- **El precio de la variante puede faltar** y hay que caer al del padre.
- El stock es un enum (`IN_STOCK` / `OUT_OF_STOCK`), no un número: guardar 0
  sería inventar, así que queda en `None`.
- `pageSize` mayor a 100 lo rechaza; hay tiendas que además limitan el
  `currentPage` máximo.
- **El esquema cambia entre versiones.** `uid` no existe en Magento < 2.4.2
  y su sola presencia hace fallar la query entera: bowen.com.ar devolvía
  cero productos por pedir un campo que su versión no conoce. Por eso hay
  una query mínima de respaldo.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..models import (
    Availability, Gender, Image, Price, Product, Variant,
)
from ..normalize.money import parse_ars
from ..normalize.taxonomy import classify_category, classify_gender
from ..normalize.text import clean, normalize_title, slugify
from ._options import repartir
from .base import StoreAdapter

PAGE = 50
MAX_PAGES = 400

# Fallback: hay tiendas que rechazan `media_gallery` o el fragmento de
# ConfigurableProduct y devuelven la respuesta entera vacía. Mejor un
# producto con menos campos que ningún producto.
QUERY_MINIMA = """
query Catalogo($page: Int!, $size: Int!) {
  products(search: "", pageSize: $size, currentPage: $page) {
    total_count
    items {
      __typename sku name url_key stock_status
      image { url }
      price_range { minimum_price { final_price { value currency }
                                    regular_price { value } } }
      ... on ConfigurableProduct {
        variants {
          attributes { code label }
          product { sku name stock_status
                    price_range { minimum_price { final_price { value } } } }
        }
      }
    }
  }
}
"""

QUERY = """
query Catalogo($page: Int!, $size: Int!) {
  products(search: "", pageSize: $size, currentPage: $page) {
    total_count
    items {
      __typename sku name url_key stock_status
      image { url }
      media_gallery { url }
      description { html }
      short_description { html }
      categories { name }
      price_range { minimum_price { final_price { value currency }
                                    regular_price { value } } }
      ... on ConfigurableProduct {
        configurable_options { label values { label } }
        variants {
          attributes { code label }
          product { sku name stock_status
                    price_range { minimum_price { final_price { value }
                                                  regular_price { value } } } }
        }
      }
    }
  }
}
"""

_STOCK = {"IN_STOCK": Availability.IN_STOCK, "OUT_OF_STOCK": Availability.OUT_OF_STOCK}


class MagentoAdapter(StoreAdapter):
    platform = "magento"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._query = QUERY

    async def _post(self, query: str, page: int) -> tuple[list[dict], int]:
        async with httpx.AsyncClient(
            headers={"User-Agent": self.fetcher._client.headers["User-Agent"],
                     "Content-Type": "application/json"},
            timeout=60.0, follow_redirects=True,
        ) as c:
            try:
                r = await c.post(f"https://{self.store.domain}/graphql",
                                 json={"query": query,
                                       "variables": {"page": page, "size": PAGE}})
            except httpx.HTTPError:
                return [], 0
        if r.status_code != 200:
            return [], 0
        try:
            d = r.json()
        except json.JSONDecodeError:
            return [], 0
        prods = (d.get("data") or {}).get("products") or {}
        return prods.get("items") or [], prods.get("total_count") or 0

    async def _page(self, page: int) -> tuple[list[dict], int]:
        items, total = await self._post(self._query, page)
        if not items and self._query is QUERY:
            # la query completa no anduvo en esta tienda: bajar a la mínima
            items, total = await self._post(QUERY_MINIMA, page)
            if items:
                self._query = QUERY_MINIMA
        return items, total

    async def store_identity(self) -> dict:
        """`base_url` y `store_name` reales de la instancia.

        babycottons.com.ar y carocuore.com.ar responden ambos
        `"Caro Cuore AR Store View"` con `base_url` carocuore.com: son el
        mismo Magento detrás de dos dominios. Sin chequear esto, el mismo
        catálogo se ingiere dos veces con `product_uid` distintos.
        """
        async with httpx.AsyncClient(
            headers={"User-Agent": self.fetcher._client.headers["User-Agent"],
                     "Content-Type": "application/json"},
            timeout=30.0, follow_redirects=True,
        ) as c:
            try:
                r = await c.post(f"https://{self.store.domain}/graphql",
                                 json={"query": "{storeConfig{store_code store_name base_url}}"})
                return (r.json().get("data") or {}).get("storeConfig") or {}
            except Exception:                            # noqa: BLE001
                return {}

    async def total(self) -> int:
        _, t = await self._page(1)
        return t

    async def discover_product_urls(self) -> list[str]:
        return [str(p.url) async for p in self.harvest()]

    async def harvest(self, limit: int | None = None) -> AsyncIterator[Product]:
        n = 0
        for page in range(1, MAX_PAGES + 1):
            items, total = await self._page(page)
            if not items:
                return
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                if (prod := self.to_product(raw)) is not None:
                    yield prod
                    n += 1
                    if limit and n >= limit:
                        return
            if page * PAGE >= total:
                return

    # ------------------------------------------------------------------ mapeo
    def to_product(self, raw: dict[str, Any]) -> Product | None:
        sku = clean(raw.get("sku") or "")
        if not sku:
            return None

        padre = self._precio(raw.get("price_range"))
        variants = [v for v in
                    (self._variant(x, padre) for x in (raw.get("variants") or [])
                     if isinstance(x, dict)) if v]
        if not variants:
            # SimpleProduct: el producto es su propia variante
            if padre is None:
                return None
            variants = [Variant(
                external_id=sku,
                sku=sku,
                price=padre,
                availability=_STOCK.get(raw.get("stock_status") or "", Availability.UNKNOWN),
                stock=None,
            )]

        precios = [v.price for v in variants]
        price_min = min(precios, key=lambda p: p.amount_cents)
        price_max = max(precios, key=lambda p: p.amount_cents)

        title = clean(raw.get("name") or "")
        breadcrumb = [clean(c.get("name", "")) for c in (raw.get("categories") or [])
                      if isinstance(c, dict) and c.get("name")]
        handle = clean(raw.get("url_key") or "") or slugify(title)
        disponible = any(v.availability is Availability.IN_STOCK for v in variants)

        imgs = []
        if isinstance(im := raw.get("image"), dict) and im.get("url"):
            imgs.append(im["url"])
        imgs += [g["url"] for g in (raw.get("media_gallery") or [])
                 if isinstance(g, dict) and g.get("url")]

        _cat = classify_category(title, breadcrumb, handle)
        return Product(
            product_uid=f"{self.store.slug}:{sku}",
            store_slug=self.store.slug,
            external_id=sku,
            url=f"https://{self.store.domain}/{handle}.html",
            handle=handle,
            title=title,
            title_normalized=normalize_title(title),
            description=self._descripcion(raw),
            brand=self.store.name,
            category_path=breadcrumb,
            category=_cat,
            gender=classify_gender(title, breadcrumb, store_default=Gender.UNKNOWN, category=_cat),
            price_min=price_min,
            price_max=price_max,
            availability=Availability.IN_STOCK if disponible else Availability.OUT_OF_STOCK,
            variants=variants,
            sizes_available=self._distinct(
                v.size.normalized or v.size.raw for v in variants
                if v.size and v.availability is Availability.IN_STOCK),
            colors_available=self._distinct(
                v.color.raw for v in variants
                if v.color and v.availability is Availability.IN_STOCK),
            images=[Image(url=u, position=i)
                    for i, u in enumerate(dict.fromkeys(imgs))][:12],
            content_hash=self._hash(title, variants),
            source="api",
            raw={k: v for k, v in raw.items() if k != "variants"},
        )

    @staticmethod
    def _descripcion(raw: dict[str, Any]) -> str | None:
        """`description` trae el texto; `short_description` suele venir vacío."""
        for campo in ("description", "short_description"):
            html = (raw.get(campo) or {}).get("html") or ""
            if txt := clean(re.sub(r"<[^>]+>", " ", html)):
                return txt
        return None

    @staticmethod
    def _precio(price_range: dict | None) -> Price | None:
        mp = (price_range or {}).get("minimum_price") or {}
        cents = parse_ars((mp.get("final_price") or {}).get("value"))
        if cents is None or cents <= 0:
            return None
        lista = parse_ars((mp.get("regular_price") or {}).get("value"))
        if not lista or lista <= cents:
            lista = None
        return Price(amount_cents=cents, currency="ARS", compare_at_cents=lista)

    def _variant(self, v: dict[str, Any], padre: Price | None) -> Variant | None:
        prod = v.get("product") or {}
        sku = clean(prod.get("sku") or "")
        if not sku:
            return None
        # el precio de la variante puede venir vacío: se hereda del padre
        precio = self._precio(prod.get("price_range")) or padre
        if precio is None:
            return None

        pares = [(clean(a.get("code") or ""), clean(a.get("label") or ""))
                 for a in (v.get("attributes") or [])
                 if isinstance(a, dict) and a.get("label")]
        size, color, extras = repartir(pares)

        return Variant(
            external_id=sku,
            sku=sku,
            size=size,
            color=color,
            price=precio,
            availability=_STOCK.get(prod.get("stock_status") or "", Availability.UNKNOWN),
            # Magento publica un enum, no una cantidad: 0 sería inventado
            stock=None,
            extra_options=extras,
        )

    @staticmethod
    def _distinct(values) -> list[str]:
        return list(dict.fromkeys(v for v in values if v))

    @staticmethod
    def _hash(title: str, variants: list[Variant]) -> str:
        payload = title + "|" + "|".join(
            f"{v.external_id}:{v.price.amount_cents}:{v.availability.value}"
            for v in sorted(variants, key=lambda x: x.external_id))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
