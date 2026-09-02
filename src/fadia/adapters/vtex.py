"""Adapter VTEX.

VTEX expone `/api/catalog_system/pub/products/search` sin autenticación.
Devuelve **206 Partial Content** y el total va en el header
`resources: 0-49/779` — no en el cuerpo. Sin leer ese header no hay forma
de saber cuántas páginas pedir.

Dos límites de la plataforma que hay que respetar o el crawl se corta solo:

- La ventana máxima es de **50 productos por request** (`_to - _from < 50`).
- El offset tope es **2.500**: pedir más allá devuelve error. Para catálogos
  grandes hay que particionar por categoría, no seguir paginando.

A cambio, el modelo de datos es el más completo de los tres: `variations`
nombra las opciones, y `commertialOffer` trae precio, precio de lista y
**cantidad disponible real** por SKU.
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
from ..normalize.text import clean, normalize_title
from ._options import repartir
from .base import StoreAdapter

PAGE = 50            # tope duro de VTEX
MAX_OFFSET = 2500    # más allá de esto la API rechaza
_RESOURCES = re.compile(r"(\d+)-(\d+)/(\d+)")

# Varias tiendas sirven el SPA en `/api/...` de su dominio público y dejan
# la API real solo en el dominio interno de VTEX. Portsaid y Tascani
# devolvían HTML con 200 y el adapter leía cero productos sin quejarse.
_CUENTA = re.compile(r'"account"\s*:\s*"([a-z0-9][a-z0-9-]{1,22})"', re.I)


class VtexAdapter(StoreAdapter):
    platform = "vtex"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._host: str | None = None        # se resuelve en el primer uso

    async def _resolver_host(self) -> str:
        """Dominio que sí sirve la API de catálogo.

        Primero el público; si devuelve HTML en vez de JSON, se busca la
        cuenta VTEX en el home y se prueba `{cuenta}.vtexcommercestable.com.br`.
        """
        if self._host:
            return self._host

        publico = self.store.domain
        lote, _ = await self._fetch(self._url_en(publico, 0, 0))
        if lote:
            self._host = publico
            return publico

        try:
            html = await self.fetcher.get(f"https://{publico}/", use_cache=False)
        except Exception:                                # noqa: BLE001
            html = ""
        for cuenta in dict.fromkeys(_CUENTA.findall(html)):
            interno = f"{cuenta}.vtexcommercestable.com.br"
            lote, _ = await self._fetch(self._url_en(interno, 0, 0))
            if lote:
                self._host = interno
                return interno

        self._host = publico
        return publico

    @staticmethod
    def _url_en(host: str, frm: int, to: int, category: str = "") -> str:
        base = f"https://{host}/api/catalog_system/pub/products/search"
        path = f"/{category}" if category else ""
        return f"{base}{path}?_from={frm}&_to={to}"

    def _url(self, frm: int, to: int, category: str = "") -> str:
        return self._url_en(self._host or self.store.domain, frm, to, category)

    async def _fetch(self, url: str) -> tuple[list[dict], int]:
        """Devuelve (productos, total). El total sale del header `resources`."""
        async with httpx.AsyncClient(
            headers={"User-Agent": self.fetcher._client.headers["User-Agent"]},
            timeout=45.0, follow_redirects=True,
        ) as c:
            r = await c.get(url)
        if r.status_code not in (200, 206):
            return [], 0
        # 200 con HTML es el SPA, no la API: tratarlo como "sin datos"
        if "json" not in r.headers.get("content-type", ""):
            return [], 0
        total = 0
        if m := _RESOURCES.search(r.headers.get("resources", "")):
            total = int(m.group(3))
        try:
            data = r.json()
        except json.JSONDecodeError:
            return [], total
        return (data if isinstance(data, list) else []), total

    async def total(self) -> int:
        await self._resolver_host()
        _, t = await self._fetch(self._url(0, 0))
        return t

    async def categories(self) -> list[str]:
        """Árbol de categorías: la vía para pasar el techo de 2.500."""
        url = f"https://{self._host or self.store.domain}/api/catalog_system/pub/category/tree/3"
        try:
            arbol = json.loads(await self.fetcher.get(url, use_cache=False))
        except Exception:                                # noqa: BLE001
            return []
        out: list[str] = []

        def rec(nodos):
            for n in nodos:
                if url_ := n.get("url"):
                    out.append(url_.split(f"{self.store.domain}/")[-1].strip("/"))
                rec(n.get("children") or [])

        rec(arbol if isinstance(arbol, list) else [])
        return [c for c in dict.fromkeys(out) if c]

    async def discover_product_urls(self) -> list[str]:
        return [str(p.url) async for p in self.harvest()]

    async def harvest(self, limit: int | None = None) -> AsyncIterator[Product]:
        await self._resolver_host()
        vistos: set[str] = set()
        n = 0
        # Primero el catálogo plano; si excede el techo, se completa por categoría.
        planes = [""]
        if (await self.total()) > MAX_OFFSET:
            planes += await self.categories()

        for cat in planes:
            frm = 0
            while frm < MAX_OFFSET:
                lote, total = await self._fetch(self._url(frm, frm + PAGE - 1, cat))
                if not lote:
                    break
                for raw in lote:
                    pid = str(raw.get("productId") or "")
                    if not pid or pid in vistos:
                        continue
                    vistos.add(pid)
                    if (prod := self.to_product(raw)) is not None:
                        yield prod
                        n += 1
                        if limit and n >= limit:
                            return
                frm += PAGE
                if frm >= total:
                    break

    # ------------------------------------------------------------------ mapeo
    def to_product(self, raw: dict[str, Any]) -> Product | None:
        pid = raw.get("productId")
        if pid is None:
            return None

        variants = [v for v in (self._variant(i) for i in raw.get("items") or []) if v]
        if not variants:
            return None

        precios = [v.price for v in variants]
        price_min = min(precios, key=lambda p: p.amount_cents)
        price_max = max(precios, key=lambda p: p.amount_cents)

        title = clean(raw.get("productName") or "")
        # '/Calzado/Zapatillas/' -> ['Calzado', 'Zapatillas']
        cats = raw.get("categories") or []
        breadcrumb = [c for c in (cats[0].strip("/").split("/") if cats else []) if c]
        handle = clean(raw.get("linkText") or "") or str(pid)
        url = raw.get("link") or f"https://{self.store.domain}/{handle}/p"
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
            description=clean(raw.get("metaTagDescription") or "") or None,
            brand=clean(raw.get("brand") or "") or self.store.name,
            category_path=breadcrumb,
            category=_cat,
            gender=classify_gender(title, breadcrumb, store_default=Gender.UNKNOWN, category=_cat),
            collections=[clean(c) for c in (raw.get("productClusters") or {}).values()][:10]
            if isinstance(raw.get("productClusters"), dict) else [],
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
            images=self._images(raw),
            content_hash=self._hash(title, variants),
            source="api",
            raw={k: v for k, v in raw.items() if k != "items"},
        )

    @staticmethod
    def _variant(item: dict[str, Any]) -> Variant | None:
        iid = item.get("itemId")
        sellers = item.get("sellers") or []
        if iid is None or not sellers:
            return None
        oferta = sellers[0].get("commertialOffer") or {}
        cents = parse_ars(oferta.get("Price"))
        if cents is None or cents <= 0:
            return None
        lista = parse_ars(oferta.get("ListPrice"))
        if not lista or lista <= cents:
            lista = None

        # `variations` nombra qué claves del item son opciones
        pares: list[tuple[str, str]] = []
        for nombre in item.get("variations") or []:
            valores = item.get(nombre)
            if isinstance(valores, list) and valores:
                pares.append((nombre, str(valores[0])))
            elif isinstance(valores, str) and valores:
                pares.append((nombre, valores))
        size, color, extras = repartir(pares)

        qty = oferta.get("AvailableQuantity")
        imgs = item.get("images") or []
        return Variant(
            external_id=str(iid),
            sku=clean(str(item.get("ean") or "")) or None,
            size=size,
            color=color,
            price=Price(amount_cents=cents, currency="ARS", compare_at_cents=lista),
            availability=(Availability.IN_STOCK if oferta.get("IsAvailable")
                          else Availability.OUT_OF_STOCK),
            stock=int(qty) if isinstance(qty, (int, float)) else None,
            image_url=imgs[0].get("imageUrl") if imgs else None,
            extra_options=extras,
        )

    @staticmethod
    def _images(raw: dict[str, Any]) -> list[Image]:
        vistas, out = set(), []
        for item in raw.get("items") or []:
            for img in item.get("images") or []:
                u = img.get("imageUrl")
                if u and u not in vistas:
                    vistas.add(u)
                    out.append(Image(url=u, position=len(out)))
        return out[:12]

    @staticmethod
    def _distinct(values) -> list[str]:
        return list(dict.fromkeys(v for v in values if v))

    @staticmethod
    def _hash(title: str, variants: list[Variant]) -> str:
        payload = title + "|" + "|".join(
            f"{v.external_id}:{v.price.amount_cents}:{v.availability.value}:{v.stock}"
            for v in sorted(variants, key=lambda x: x.external_id))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
