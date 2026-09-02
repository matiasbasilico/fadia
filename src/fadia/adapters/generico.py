"""Extractor genérico: datos estructurados, sin saber la plataforma.

Es el escalón que va debajo de los adapters de plataforma. No sabe si el
sitio es Shopify o un WordPress a medida: solo lee lo que el sitio publica
para Google — JSON-LD `Product`, microdata schema.org y, como último
recurso, las etiquetas OpenGraph de producto.

**Lo que rinde, medido:** se sondearon 25 marcas argentinas que ningún
adapter de plataforma cubre. Solo 2 publican JSON-LD `Product` y una sola
incluye el precio. O sea que este escalón NO rescata la cola larga
argentina — la mayoría de esas tiendas son SPAs sin datos estructurados o
están detrás de protección anti-bot.

Sirve igual, y por eso está: cubre los sitios que sí hacen bien el SEO sin
escribir un adapter por cada uno, y es el default razonable para cualquier
dominio nuevo antes de invertir en código específico.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..models import (
    Availability, Gender, Image, Price, Product, Variant,
)
from ..normalize.color import normalize_color
from ..normalize.money import parse_ars
from ..normalize.size import normalize_size
from ..normalize.taxonomy import classify_category, classify_gender
from ..normalize.text import clean, normalize_title, slugify
from .base import StoreAdapter

_LDJSON = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']', re.I)

SITEMAPS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
            "/wp-sitemap.xml")

# Pistas de que una URL es ficha de producto. NO se usan para filtrar
# —muchas tiendas usan rutas planas— sino para ORDENAR: probar primero lo
# probable evita recorrer cientos de páginas institucionales antes de dar
# con el primer producto.
_PISTA = re.compile(r"/(producto|productos|product|products|item|shop|tienda|p)/", re.I)
_DESCARTE = re.compile(
    r"/(blog|nota|noticias?|prensa|ayuda|faq|contacto|terminos|privacidad|"
    r"cuenta|carrito|checkout|login|categoria|category|coleccion|collections?)/|"
    r"\.(pdf|jpg|png|webp)$", re.I)
MAX_HIJOS = 12          # sitemaps hijos a seguir
MAX_URLS = 8000

_DISPONIBLE = {
    "instock": Availability.IN_STOCK, "in_stock": Availability.IN_STOCK,
    "limitedavailability": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK, "soldout": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER, "backorder": Availability.PREORDER,
}


class GenericoAdapter(StoreAdapter):
    """Descubre por sitemap y extrae de datos estructurados."""

    platform = "generico"

    # ------------------------------------------------------------------ descubrimiento
    async def discover_product_urls(self) -> list[str]:
        base = f"https://{self.store.domain}"
        for ruta in SITEMAPS:
            try:
                xml = await self.fetcher.get(urljoin(base, ruta))
            except Exception:                            # noqa: BLE001
                continue
            if "<loc>" not in xml:
                continue
            locs = _LOC.findall(xml)
            hijos = [u for u in locs if u.endswith((".xml", ".xml.gz"))]
            urls = [u for u in locs if u not in hijos]
            for h in hijos[:MAX_HIJOS]:
                try:
                    urls += [u for u in _LOC.findall(await self.fetcher.get(h))
                             if not u.endswith((".xml", ".xml.gz"))]
                except Exception:                        # noqa: BLE001
                    continue
                if len(urls) >= MAX_URLS:
                    break
            return self._ordenar(dict.fromkeys(urls))[:MAX_URLS]
        return []

    @staticmethod
    def _ordenar(urls) -> list[str]:
        probables, resto = [], []
        for u in urls:
            if _DESCARTE.search(u):
                continue
            (probables if _PISTA.search(u) else resto).append(u)
        return probables + resto

    # ------------------------------------------------------------------ parseo
    async def parse_html(self, url: str, html: str) -> Product | None:
        datos = self._producto_ld(html) or self._producto_microdata(html)
        og = self._og(html)
        if not datos and og.get("og:type", "").startswith("product"):
            datos = self._de_og(og)
        if not datos:
            return None

        title = clean(self._texto(datos.get("name")))
        if not title or len(title) < 2:
            return None

        ofertas = self._ofertas(datos)
        precios = [p for p in (parse_ars(o.get("price")) for o in ofertas) if p]
        if not precios:
            return None
        cents = min(precios)

        disp = Availability.UNKNOWN
        for o in ofertas:
            a = str(o.get("availability") or "").rsplit("/", 1)[-1].lower()
            if a in _DISPONIBLE:
                disp = _DISPONIBLE[a]
                break

        breadcrumb = self._breadcrumb(html)
        handle = urlparse(url).path.strip("/").split("/")[-1] or slugify(title)
        marca = self._texto((datos.get("brand") or {}) if isinstance(datos.get("brand"), dict)
                            else datos.get("brand"))

        variants = self._variantes(datos, ofertas, cents, disp)

        _cat = classify_category(title, breadcrumb, handle)
        return Product(
            product_uid=f"{self.store.slug}:{self._id(datos, url)}",
            store_slug=self.store.slug,
            external_id=self._id(datos, url),
            url=url,
            handle=handle,
            title=title,
            title_normalized=normalize_title(title),
            description=clean(self._texto(datos.get("description"))) or None,
            brand=clean(marca) or self.store.name,
            category_path=breadcrumb,
            category=_cat,
            gender=classify_gender(title, breadcrumb, store_default=Gender.UNKNOWN, category=_cat),
            price_min=Price(amount_cents=cents, currency="ARS"),
            price_max=Price(amount_cents=max(precios), currency="ARS"),
            availability=disp,
            variants=variants,
            sizes_available=self._distinct(
                v.size.normalized or v.size.raw for v in variants
                if v.size and v.availability is not Availability.OUT_OF_STOCK),
            colors_available=self._distinct(
                v.color.raw for v in variants
                if v.color and v.availability is not Availability.OUT_OF_STOCK),
            images=self._imagenes(datos, og, url),
            content_hash=hashlib.sha256(
                f"{title}|{cents}|{disp.value}|{len(variants)}".encode()).hexdigest()[:16],
            source="jsonld",
            raw=datos,
        )

    # ------------------------------------------------------------------ fuentes
    @classmethod
    def _producto_ld(cls, html: str) -> dict | None:
        """Primer nodo `Product` del JSON-LD, incluido dentro de `@graph`."""
        for bloque in _LDJSON.findall(html):
            try:
                d = json.loads(bloque.strip())
            except json.JSONDecodeError:
                continue
            for nodo in cls._aplanar(d):
                t = nodo.get("@type")
                tipos = t if isinstance(t, list) else [t]
                if any("Product" in str(x) for x in tipos):
                    return nodo
        return None

    @staticmethod
    def _aplanar(d: Any) -> list[dict]:
        out: list[dict] = []
        pila = [d]
        while pila:
            x = pila.pop()
            if isinstance(x, list):
                pila.extend(x)
            elif isinstance(x, dict):
                out.append(x)
                if g := x.get("@graph"):
                    pila.append(g)
                if m := x.get("mainEntity"):
                    pila.append(m)
        return out

    @staticmethod
    def _producto_microdata(html: str) -> dict | None:
        """Microdata schema.org/Product: `itemprop` sueltos en el markup."""
        if "schema.org/Product" not in html:
            return None
        tree = HTMLParser(html)
        cont = next((n for n in tree.css("[itemtype]")
                     if "schema.org/Product" in (n.attributes.get("itemtype") or "")), None)
        if cont is None:
            return None
        campos: dict[str, Any] = {}
        for n in cont.css("[itemprop]"):
            prop = n.attributes.get("itemprop")
            val = (n.attributes.get("content") or n.attributes.get("src")
                   or n.attributes.get("href") or clean(n.text()))
            if prop and val and prop not in campos:
                campos[prop] = val
        if not campos.get("name"):
            return None
        return {"@type": "Product", "name": campos.get("name"),
                "description": campos.get("description"),
                "image": campos.get("image"),
                "brand": campos.get("brand"),
                "offers": {"price": campos.get("price"),
                           "priceCurrency": campos.get("priceCurrency"),
                           "availability": campos.get("availability")}}

    @staticmethod
    def _og(html: str) -> dict[str, str]:
        return {k.lower(): v for k, v in _META.findall(html)}

    @staticmethod
    def _de_og(og: dict[str, str]) -> dict:
        return {"@type": "Product", "name": og.get("og:title"),
                "description": og.get("og:description"),
                "image": og.get("og:image"),
                "brand": og.get("og:site_name"),
                "offers": {"price": og.get("product:price:amount") or og.get("og:price:amount"),
                           "priceCurrency": og.get("product:price:currency"),
                           "availability": og.get("product:availability")}}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _texto(v: Any) -> str:
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return str(v.get("name") or v.get("@value") or "")
        if isinstance(v, list) and v:
            return GenericoAdapter._texto(v[0])
        return ""

    @staticmethod
    def _ofertas(datos: dict) -> list[dict]:
        of = datos.get("offers") or {}
        if isinstance(of, dict):
            # AggregateOffer envuelve las ofertas reales
            if of.get("offers"):
                inner = of["offers"]
                return inner if isinstance(inner, list) else [inner]
            if of.get("lowPrice") and not of.get("price"):
                of = {**of, "price": of["lowPrice"]}
            return [of]
        return [o for o in of if isinstance(o, dict)]

    def _variantes(self, datos: dict, ofertas: list[dict], cents: int,
                   disp: Availability) -> list[Variant]:
        out: list[Variant] = []
        for i, o in enumerate(ofertas):
            c = parse_ars(o.get("price")) or cents
            nombre = clean(self._texto(o.get("name") or o.get("sku") or ""))
            a = str(o.get("availability") or "").rsplit("/", 1)[-1].lower()
            out.append(Variant(
                external_id=str(o.get("sku") or o.get("@id") or i),
                sku=clean(str(o.get("sku") or "")) or None,
                size=normalize_size(nombre) if nombre else None,
                color=normalize_color(nombre) if nombre else None,
                price=Price(amount_cents=c, currency="ARS"),
                availability=_DISPONIBLE.get(a, disp),
                stock=None,
            ))
        return out or [Variant(external_id="0",
                               price=Price(amount_cents=cents, currency="ARS"),
                               availability=disp)]

    @classmethod
    def _breadcrumb(cls, html: str) -> list[str]:
        for bloque in _LDJSON.findall(html):
            try:
                d = json.loads(bloque.strip())
            except json.JSONDecodeError:
                continue
            for nodo in cls._aplanar(d):
                if "BreadcrumbList" in str(nodo.get("@type")):
                    return [clean(cls._texto(i.get("name") or i.get("item")))
                            for i in (nodo.get("itemListElement") or [])
                            if isinstance(i, dict)][1:-1]
        return []

    @staticmethod
    def _imagenes(datos: dict, og: dict, url: str) -> list[Image]:
        crudas = datos.get("image") or og.get("og:image") or []
        if isinstance(crudas, (str, dict)):
            crudas = [crudas]
        urls = []
        for x in crudas:
            u = x if isinstance(x, str) else (x or {}).get("url") or (x or {}).get("contentUrl")
            if isinstance(u, str) and u.strip():
                urls.append(urljoin(url, u.strip()))
        return [Image(url=u, position=i)
                for i, u in enumerate(dict.fromkeys(urls))][:12]

    @staticmethod
    def _id(datos: dict, url: str) -> str:
        for k in ("sku", "productID", "mpn", "@id"):
            if v := datos.get(k):
                if isinstance(v, str) and v.strip():
                    return slugify(v)[:80] or slugify(url)[-80:]
        return slugify(urlparse(url).path)[-80:] or slugify(url)[-80:]

    @staticmethod
    def _distinct(values) -> list[str]:
        return list(dict.fromkeys(v for v in values if v))

    # ------------------------------------------------------------------ cosecha
    async def harvest(self, limit: int | None = None) -> AsyncIterator[Product]:
        """Recorre el sitemap y descarta lo que no sea ficha de producto.

        Un sitemap trae categorías, notas y páginas institucionales
        mezcladas con los productos. No se filtra por patrón de URL —muchas
        tiendas usan rutas planas— sino por si la página publica un
        `Product`: el filtro más barato es el propio parseo.
        """
        urls = await self.discover_product_urls()
        vistos: set[str] = set()
        n = 0
        fallidas = 0
        for url in urls:
            # si 60 páginas seguidas no son fichas, este sitio no publica
            # datos estructurados y seguir es tirar requests
            if fallidas >= 60 and n == 0:
                return
            try:
                p = await self.parse_product(url)
            except Exception:                            # noqa: BLE001
                continue
            if p is None:
                fallidas += 1
                continue
            fallidas = 0
            if p.product_uid in vistos:
                continue
            vistos.add(p.product_uid)
            yield p
            n += 1
            if limit and n >= limit:
                return
