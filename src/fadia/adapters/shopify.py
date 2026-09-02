"""Adapter Shopify.

El más barato de los tres: `/products.json` es un endpoint público y
paginado que devuelve el catálogo completo con variantes, precios y
opciones **con nombre**. No hay HTML de por medio.

Dos trampas que cuestan datos si no se miran:

- **`compare_at_price` viene "0.00", no `null`,** cuando no hay descuento.
  Tomarlo literal produce productos con precio de lista cero y descuentos
  del 100 %.
- **`products.json` no trae stock por variante** (`inventory_quantity` es
  `null`): el único dato de disponibilidad es el booleano `available`.
  Guardar 0 como stock sería inventar; se deja en `None`.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from ..models import (
    Availability, Gender, Image, Price, Product, Variant,
)
from ..normalize.money import parse_ars
from ..normalize.taxonomy import classify_category, classify_gender
from ..normalize.text import clean, normalize_title
from ._options import repartir
from .base import StoreAdapter

PAGE_SIZE = 250          # tope que acepta Shopify
MAX_PAGES = 400          # 100.000 productos: cortafuegos, no un límite real


class ShopifyAdapter(StoreAdapter):
    platform = "shopify"

    # ------------------------------------------------------------------ descubrimiento
    async def _page(self, page: int) -> list[dict[str, Any]]:
        url = (f"https://{self.store.domain}/products.json"
               f"?limit={PAGE_SIZE}&page={page}")
        import json
        try:
            data = json.loads(await self.fetcher.get(url, use_cache=False))
        except (ValueError, Exception):                  # noqa: BLE001
            return []
        return data.get("products", []) if isinstance(data, dict) else []

    async def discover_product_urls(self) -> list[str]:
        return [f"https://{self.store.domain}/products/{p['handle']}"
                async for p in self._iter_raw()]

    async def _iter_raw(self) -> AsyncIterator[dict[str, Any]]:
        for page in range(1, MAX_PAGES + 1):
            lote = await self._page(page)
            if not lote:
                return
            for p in lote:
                yield p
            if len(lote) < PAGE_SIZE:
                return

    # ------------------------------------------------------------------ cosecha
    async def harvest(self, limit: int | None = None) -> AsyncIterator[Product]:
        n = 0
        async for raw in self._iter_raw():
            if (prod := self.to_product(raw)) is not None:
                yield prod
                n += 1
                if limit and n >= limit:
                    return

    # ------------------------------------------------------------------ mapeo
    def to_product(self, raw: dict[str, Any]) -> Product | None:
        pid = raw.get("id")
        handle = raw.get("handle")
        if pid is None or not handle:
            return None

        # nombres de las opciones: option1 -> options[0].name, etc.
        nombres = [o.get("name", "") for o in (raw.get("options") or [])]
        variants = [v for v in (self._variant(v, nombres) for v in raw.get("variants") or [])
                    if v]
        if not variants:
            return None

        precios = [v.price for v in variants]
        price_min = min(precios, key=lambda p: p.amount_cents)
        price_max = max(precios, key=lambda p: p.amount_cents)

        title = clean(raw.get("title") or "")
        breadcrumb = [clean(raw.get("product_type") or "")] if raw.get("product_type") else []
        tags = [clean(t) for t in (raw.get("tags") or []) if clean(t)]
        url = f"https://{self.store.domain}/products/{handle}"
        disponible = any(v.availability is Availability.IN_STOCK for v in variants)

        _cat = classify_category(title, breadcrumb, handle)
        return Product(
            product_uid=f"{self.store.slug}:{pid}",
            store_slug=self.store.slug,
            external_id=str(pid),
            url=url,
            handle=handle,
            title=title,
            title_normalized=normalize_title(title),
            description=clean(self._strip(raw.get("body_html") or "")) or None,
            brand=clean(raw.get("vendor") or "") or self.store.name,
            category_path=breadcrumb,
            category=_cat,
            gender=classify_gender(title, breadcrumb + tags, store_default=Gender.UNKNOWN, category=_cat),
            tags=tags[:20],
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
            images=[Image(url=i["src"], position=i.get("position", n),
                          width=i.get("width"), height=i.get("height"))
                    for n, i in enumerate(raw.get("images") or []) if i.get("src")][:12],
            content_hash=self._hash(title, variants),
            source="api",
            raw=raw,
        )

    @staticmethod
    def _variant(v: dict[str, Any], nombres: list[str]) -> Variant | None:
        vid = v.get("id")
        cents = parse_ars(v.get("price"))
        if vid is None or cents is None:
            return None

        # "0.00" es el "sin descuento" de Shopify, no un precio de lista de $0
        compare = parse_ars(v.get("compare_at_price"))
        if not compare or compare <= cents:
            compare = None

        opciones = [(nombres[i] if i < len(nombres) else f"option{i+1}",
                     v.get(f"option{i+1}"))
                    for i in range(3)]
        size, color, extras = repartir([(n, val) for n, val in opciones if val])

        return Variant(
            external_id=str(vid),
            sku=clean(v.get("sku") or "") or None,
            size=size,
            color=color,
            price=Price(amount_cents=cents, currency="ARS", compare_at_cents=compare),
            availability=(Availability.IN_STOCK if v.get("available")
                          else Availability.OUT_OF_STOCK),
            # products.json no publica stock por variante: None, no 0
            stock=None,
            image_url=(v.get("featured_image") or {}).get("src") if v.get("featured_image") else None,
            extra_options=extras,
        )

    @staticmethod
    def _strip(html: str) -> str:
        import re
        return re.sub(r"<[^>]+>", " ", html)

    @staticmethod
    def _distinct(values) -> list[str]:
        return list(dict.fromkeys(v for v in values if v))

    @staticmethod
    def _hash(title: str, variants: list[Variant]) -> str:
        payload = title + "|" + "|".join(
            f"{v.external_id}:{v.price.amount_cents}:{v.availability.value}"
            for v in sorted(variants, key=lambda x: x.external_id))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
