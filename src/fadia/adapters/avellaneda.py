"""Adapter de Avellaneda a un Toque (avellanedaauntoque.com).

Marketplace mayorista de Av. Avellaneda, Flores: 139.044 publicaciones de
3.856 locales. No es una tienda, es un agregador — el vendedor va en
`Seller`, no en `store_slug`.

Diferencias de fondo con Tiendanube:

- **Next.js con datos client-side.** El payload RSC solo trae config del
  tenant; los datos del producto los pide el front a `/api/`, que
  `robots.txt` prohíbe. Se usa el JSON-LD server-rendered, que sí es
  público y completo.
- **Los listados por rubro se randomizan en cada request.** Tres llamadas
  seguidas a `/r/mujer/jeans` devuelven 60 productos distintos, sin
  solapamiento. Son un feed de descubrimiento, no un índice: la única vía
  al 100% del catálogo es el sitemap.
- **Doble precio.** `AggregateOffer.lowPrice` es el precio por mayor y
  `highPrice` el de por menor, más una compra mínima ("curva") que define
  cuánto hay que llevar. Publicar solo un número miente sobre el costo real.
- **`lastmod` por producto** en el sitemap: habilita crawl incremental, que
  a 139k fichas es la diferencia entre 38 horas y unos minutos por corrida.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..models import (
    Availability, Gender, Image, Price, Product, Seller, Variant,
)
from ..normalize.money import parse_ars
from ..normalize.taxonomy import classify_category, classify_gender
from ..normalize.text import clean, normalize_title, slugify
from .base import StoreAdapter

_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_URL_ENTRY = re.compile(r"<url>\s*<loc>\s*([^<\s]+)\s*</loc>\s*(?:<lastmod>\s*([^<\s]+)\s*</lastmod>)?", re.S)
_LDJSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_PROD_URL = re.compile(r"/p/([A-Za-z0-9]+)(?:-(.*))?$")

_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER,
}


class AvellanedaAdapter(StoreAdapter):
    platform = "avellaneda"

    # ------------------------------------------------------------------ descubrimiento
    async def discover_product_urls(self) -> list[str]:
        return [u for u, _ in await self.discover_with_lastmod()]

    async def discover_with_lastmod(self) -> list[tuple[str, str | None]]:
        """Catálogo completo con fecha de modificación, desde el índice de
        sitemaps. 14 archivos de productos, 10.000 URLs cada uno."""
        base = f"https://{self.store.domain}"
        index = await self.fetcher.get(urljoin(base, "/sitemap.xml"))
        children = [u for u in _SITEMAP_LOC.findall(index) if "/p/sitemap/" in u]

        out: list[tuple[str, str | None]] = []
        for child in sorted(children, key=_sitemap_order):
            xml = await self.fetcher.get(child)
            out.extend((u, lm or None) for u, lm in _URL_ENTRY.findall(xml))
        return out

    async def discover_sellers(self) -> list[str]:
        base = f"https://{self.store.domain}"
        index = await self.fetcher.get(urljoin(base, "/sitemap.xml"))
        urls: list[str] = []
        for child in _SITEMAP_LOC.findall(index):
            if "/s/sitemap" not in child:
                continue
            urls.extend(_SITEMAP_LOC.findall(await self.fetcher.get(child)))
        return urls

    async def discover_categories(self) -> list[str]:
        """El sitio ya trae su propia taxonomía en /r/{genero}/{rubro}.
        Es mejor que cualquier clasificador nuestro: la escribió el operador."""
        base = f"https://{self.store.domain}"
        index = await self.fetcher.get(urljoin(base, "/sitemap.xml"))
        for child in _SITEMAP_LOC.findall(index):
            if "/r/sitemap" in child:
                return _SITEMAP_LOC.findall(await self.fetcher.get(child))
        return []

    # ------------------------------------------------------------------ parseo
    @staticmethod
    def peek_from_url(url: str) -> dict[str, str] | None:
        """Identidad y nombre tentativo sin pegarle al servidor.

        La URL es `/p/{id}-{slug}`: con 139.044 fichas, poder inventariar el
        catálogo entero desde el sitemap —sin una sola descarga de página—
        es la diferencia entre un índice en minutos y uno en 38 horas.
        """
        m = _PROD_URL.search(urlparse(url).path.rstrip("/"))
        if not m:
            return None
        pid, slug = m.group(1), (m.group(2) or "")
        return {
            "external_id": pid,
            "handle": slug,
            "title_guess": clean(slug.replace("-", " ")).title(),
            "url": url,
        }

    async def parse_html(self, url: str, html: str) -> Product | None:
        ident = self.peek_from_url(url)
        if not ident:
            return None

        ld = self._product_ld(html)
        if not ld:
            return None

        tree = HTMLParser(html)
        text = self._visible_text(tree)

        title = clean(ld.get("name") or ident["title_guess"])
        offers = ld.get("offers") or {}
        wholesale = parse_ars(offers.get("lowPrice") or offers.get("price"))
        retail_ld = parse_ars(offers.get("highPrice"))
        retail_txt = self._retail_from_text(text)
        if wholesale is None:
            return None

        # El texto visible manda sobre highPrice: highPrice a veces es el
        # techo de un rango de variantes, no el precio por menor.
        retail = retail_txt or (retail_ld if retail_ld and retail_ld != wholesale else None)

        raw_min = self._min_purchase(text)
        min_qty, min_unit = self._parse_min(raw_min)
        seller = self._seller(ld, text)
        breadcrumb = self._breadcrumb(html) or self._rubro(text)
        category = classify_category(title, breadcrumb, ident["handle"])

        price = Price(amount_cents=wholesale, currency="ARS")
        images = [Image(url=u, position=i)
                  for i, u in enumerate(self._images(ld))]

        _cat = classify_category(title, breadcrumb, ident["handle"])
        return Product(
            product_uid=f"{self.store.slug}:{ident['external_id']}",
            store_slug=self.store.slug,
            external_id=ident["external_id"],
            url=url,
            handle=ident["handle"] or slugify(title),
            title=title,
            title_normalized=normalize_title(title),
            description=clean(ld.get("description")) or None,
            brand=(ld.get("brand") or {}).get("name") or (seller.name if seller else None),
            category_path=breadcrumb,
            category=category,
            gender=classify_gender(title, breadcrumb, store_default=Gender.UNKNOWN, category=_cat),
            price_min=price,
            price_max=Price(amount_cents=retail or wholesale, currency="ARS"),
            price_retail=Price(amount_cents=retail, currency="ARS") if retail else None,
            min_purchase=raw_min,
            min_qty=min_qty,
            min_unit=min_unit,
            tags=[t for t in (self._min_purchase_shipping(text),) if t],
            seller=seller,
            availability=_AVAILABILITY.get(
                str(offers.get("availability", "")).rsplit("/", 1)[-1].lower(),
                Availability.UNKNOWN),
            variants=[Variant(
                external_id=ident["external_id"],
                price=price,
                availability=_AVAILABILITY.get(
                    str(offers.get("availability", "")).rsplit("/", 1)[-1].lower(),
                    Availability.UNKNOWN),
            )],
            images=images,
            content_hash=hashlib.sha256(
                f"{title}|{wholesale}|{retail}|{len(images)}".encode()).hexdigest()[:16],
            source="jsonld",
            raw={"jsonld": ld},
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _product_ld(html: str) -> dict | None:
        for block in _LDJSON.findall(html):
            try:
                d = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if d.get("@type") == "Product":
                return d
        return None

    @staticmethod
    def _visible_text(tree: HTMLParser) -> str:
        for tag in ("script", "style", "noscript"):
            for node in tree.css(tag):
                node.decompose()
        body = tree.css_first("body")
        if not body:
            return ""
        # separator obligatorio: sin el, selectolax pega los nodos vecinos
        # ("2.359Seguidores", "Compra minima:4(CURVA)5 tienda aprobada") y
        # cualquier regex con \s+ deja de matchear en silencio.
        return re.sub(r"[ \t]+", " ", body.text(separator="\n")).strip()

    @staticmethod
    def _retail_from_text(text: str) -> int | None:
        m = re.search(r"Precio\s+por\s+menor\s*:?\s*\$?\s*([\d.,]+)", text, re.I)
        return parse_ars(m.group(1)) if m else None

    @staticmethod
    def _min_purchase(text: str) -> str | None:
        """'Compra mínima: 4 (CURVA)'. En un mayorista es tan determinante
        como el precio: no podés llevar una unidad."""
        m = re.search(r"Compra\s+m[ií]nima\s*:\s*\n?([^\n]{1,60})", text, re.I)
        return clean(m.group(1)) if m else None

    @staticmethod
    def _parse_min(raw: str | None) -> tuple[int | None, str | None]:
        """La compra mínima es texto libre que tipea cada local: conviven
        '4(CURVA)', 'x curva', '12 pares' y locales que metieron ahí las
        condiciones de envío. Se extrae lo estructurable y se conserva el
        crudo: inventar un número donde el local no lo puso contamina.
        """
        if not raw:
            return None, None
        low = raw.lower()
        qty = None
        if m := re.search(r"\b(\d{1,3})\b", low[:40]):
            n = int(m.group(1))
            if 1 <= n <= 200:
                qty = n
        for unit in ("curva", "pares", "unidades", "prendas", "docena", "art"):
            if unit in low:
                return qty, unit
        return qty, None

    @staticmethod
    def _min_purchase_shipping(text: str) -> str | None:
        m = re.search(r"Compra\s+m[ií]nima\s+para\s+env[ií]os\s*:\s*\n?([^\n]{1,60})",
                      text, re.I)
        return clean(m.group(1)) if m else None

    @staticmethod
    def _rubro(text: str) -> list[str]:
        """El sitio publica su propia taxonomía en la ficha ('Rubro: ...').
        Vale más que cualquier clasificador nuestro: la escribió el operador."""
        m = re.search(r"Rubro\s*:\s*\n?([^\n]{1,60})", text, re.I)
        return [clean(m.group(1))] if m else []

    @staticmethod
    def _seller(ld: dict, text: str) -> Seller | None:
        s = ((ld.get("offers") or {}).get("seller")) or {}
        name = clean(s.get("name") or (ld.get("brand") or {}).get("name") or "")
        if not name:
            return None
        addr = s.get("address") or {}
        followers = None
        if m := re.search(r"([\d.]+)\s*\n?\s*Seguidores", text):
            followers = int(m.group(1).replace(".", ""))
        return Seller(
            name=name,
            address=clean(addr.get("streetAddress")) or None,
            verified="Verificado por" in text or "tienda aprobada" in text,
            followers=followers,
        )

    @staticmethod
    def _breadcrumb(html: str) -> list[str]:
        for block in _LDJSON.findall(html):
            try:
                d = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if d.get("@type") == "BreadcrumbList":
                return [clean(i.get("name", ""))
                        for i in d.get("itemListElement", []) if i.get("name")]
        return []

    @staticmethod
    def _images(ld: dict) -> list[str]:
        img = ld.get("image")
        if isinstance(img, str):
            return [img]
        return [u for u in (img or []) if isinstance(u, str)][:12]


def _sitemap_order(url: str) -> int:
    m = re.search(r"/(\d+)\.xml$", url)
    return int(m.group(1)) if m else 0
