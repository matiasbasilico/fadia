"""Adapter Tiendanube / Nuvemshop.

Tres fuentes de datos en la misma página, en orden de confianza:

1. `data-variants` (JSON embebido en el form de compra) — fuente de verdad
   de precio, stock, SKU y opciones. Es lo que consume el JS del tema.
2. JSON-LD schema.org — buena para descripción y breadcrumb, pero su
   `offers.price` publica el precio DE LISTA cuando hay promo: usarla para
   precio da lecturas infladas. Solo fallback.
3. HTML — labels de las opciones y galería de imágenes.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..models import (
    Availability, Gender, Image, Price, Product, Store, Variant,
)
from ..normalize.color import normalize_color
from ..normalize.money import parse_ars
from ..normalize.size import looks_like_size, normalize_size
from ..normalize.taxonomy import classify_category, classify_gender, is_collection
from ..normalize.text import clean, normalize_title
from .base import StoreAdapter

_VARIANTS_RE = re.compile(r'data-variants=(["\'])(.*?)\1', re.S)
_LSPRODUCT_RE = re.compile(r"LS\.product\s*=\s*\{(.*?)\n\s*\}", re.S)
_LS_ID_RE = re.compile(r"id\s*:\s*(\d+)")
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_LABEL_RE = re.compile(r'<label[^>]*for="variation_(\d+)"[^>]*>(.*?)</label>', re.S)
_FORM_RE = re.compile(r"<form[^>]*js-ajax-cart-panel[^>]*>(?:(?!</form>).)*</form>", re.S)


class TiendanubeAdapter(StoreAdapter):
    platform = "tiendanube"

    # ------------------------------------------------------------------ descubrimiento
    async def discover_product_urls(self) -> list[str]:
        """El sitemap trae el catálogo completo sin paginar. Siempre preferilo
        a recorrer /productos/?page=N: menos requests y sin productos perdidos."""
        base = f"https://{self.store.domain}"
        xml = await self.fetcher.get(urljoin(base, "/sitemap.xml"))
        urls = [u for u in _LOC_RE.findall(xml) if "/productos/" in u]
        # dedup preservando orden
        return list(dict.fromkeys(u for u in urls if not u.rstrip("/").endswith("/productos")))

    async def discover_collections(self) -> list[str]:
        base = f"https://{self.store.domain}"
        xml = await self.fetcher.get(urljoin(base, "/sitemap.xml"))
        out = []
        for u in _LOC_RE.findall(xml):
            seg = urlparse(u).path.strip("/")
            if seg and "/" not in seg and not seg.endswith(".xml"):
                out.append(seg)
        return out

    # ------------------------------------------------------------------ parseo
    async def parse_html(self, url: str, html: str) -> Product | None:
        tree = HTMLParser(html)
        if self._es_404(url, html):
            return None

        variants_raw = self._extract_variants(html)
        jsonld = self._extract_jsonld(html, url)
        if not variants_raw and not jsonld:
            return None

        external_id = self._external_id(html, variants_raw)
        if not external_id:
            return None

        option_labels = self._option_labels(html, variants_raw)
        breadcrumb = self._breadcrumb(jsonld)
        title = clean(self._title(jsonld, tree))
        handle = urlparse(url).path.strip("/").split("/")[-1]

        variants = [self._to_variant(v, option_labels) for v in variants_raw]
        variants = [v for v in variants if v]

        prices = [v.price for v in variants] or [self._price_from_jsonld(jsonld)]
        prices = [p for p in prices if p]
        if not prices:
            return None
        price_min = min(prices, key=lambda p: p.amount_cents)
        price_max = max(prices, key=lambda p: p.amount_cents)

        available = any(v.availability == Availability.IN_STOCK for v in variants)
        cats = [c for c in breadcrumb[1:-1] if not is_collection(c)]

        _cat = classify_category(title, breadcrumb, handle)
        return Product(
            product_uid=f"{self.store.slug}:{external_id}",
            store_slug=self.store.slug,
            external_id=external_id,
            url=url,
            handle=handle,
            title=title,
            title_normalized=normalize_title(title),
            description=self._description(jsonld, tree),
            brand=self._brand(html) or self.store.name,
            category_path=breadcrumb,
            category=_cat,
            gender=classify_gender(title, breadcrumb, store_default=Gender.WOMEN, category=_cat),
            collections=[c for c in breadcrumb[1:-1] if is_collection(c)],
            price_min=price_min,
            price_max=price_max,
            availability=Availability.IN_STOCK if available else Availability.OUT_OF_STOCK,
            variants=variants,
            sizes_available=self._distinct(
                v.size.normalized or v.size.raw for v in variants
                if v.size and v.availability == Availability.IN_STOCK
            ),
            colors_available=self._distinct(
                v.color.raw for v in variants
                if v.color and v.availability == Availability.IN_STOCK
            ),
            images=self._images(html, tree),
            content_hash=self._hash(variants, title),
            source="html+jsonld",
            raw={"variants": variants_raw, "jsonld": jsonld},
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _es_404(url: str, html: str) -> bool:
        """Tiendanube sirve su página de error con HTTP 200.

        Los sitemaps arrastran URLs muertas: valdez.com.ar tenía tres, y
        las tres se parseaban como un mismo producto llamado "Error - 404"
        con el `product_uid` de la propia página de error. El arnés lo
        frenó por `identidad:duplicados`, pero el lugar de arreglarlo es acá.

        Se compara el handle pedido contra el que declara el JSON-LD: si la
        página habla de otro producto (o de ninguno), no es esta ficha.
        """
        pedido = urlparse(url).path.strip("/").split("/")[-1].lower()
        if not pedido:
            return False
        for block in re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
        ):
            try:
                d = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if d.get("@type") != "WebPage":
                continue
            ident = str((d.get("mainEntity") or {}).get("@id", ""))
            if not ident:
                continue
            declarado = urlparse(ident).path.strip("/").split("/")[-1].lower()
            if declarado == pedido:
                return False
        return True

    @staticmethod
    def _extract_variants(html: str) -> list[dict]:
        """El primer data-variants del documento es el del producto de la
        ficha; los siguientes son de los carruseles de recomendados."""
        m = _VARIANTS_RE.search(html)
        if not m:
            return []
        try:
            data = json.loads(htmllib.unescape(m.group(2)))
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _extract_jsonld(html: str, url: str) -> dict:
        """Devuelve el Product del producto de ESTA url. La página incluye
        JSON-LD de los recomendados también, así que hay que filtrar por @id."""
        blocks = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
        )
        target = url.rstrip("/")
        fallback: dict = {}
        for b in blocks:
            try:
                d = json.loads(b.strip())
            except json.JSONDecodeError:
                continue
            if d.get("@type") == "WebPage":
                main = d.get("mainEntity") or {}
                if str(main.get("@id", "")).rstrip("/") == target:
                    return {**d, "_product": main}
                fallback = fallback or {**d, "_product": main}
        return fallback

    @staticmethod
    def _external_id(html: str, variants: list[dict]) -> str | None:
        if variants and variants[0].get("product_id"):
            return str(variants[0]["product_id"])
        if m := _LSPRODUCT_RE.search(html):
            if idm := _LS_ID_RE.search(m.group(1)):
                return idm.group(1)
        return None

    @staticmethod
    def _option_labels(html: str, variants: list[dict]) -> dict[int, str]:
        """option0/1/2 son POSICIONALES, no semánticos: en un vestido option0
        es el color y en un jean es el talle. Sin este mapeo, el dataset queda
        con talles en el campo color. Se lee del form de compra; si el tema no
        expone labels, se infiere por los valores."""
        labels: dict[int, str] = {}
        # Hay varios <form js-ajax-cart-panel> por página (modales de compra
        # rápida, carruseles de recomendados) y el primero NO es el del
        # producto: en sunnyclothing arranca 97.000 caracteres antes de los
        # labels reales. Se busca el primero que efectivamente los declare.
        for form in _FORM_RE.finditer(html):
            found = {int(m.group(1)) - 1: clean(re.sub(r"<[^>]+>", "", m.group(2))).lower()
                     for m in _LABEL_RE.finditer(form.group(0))}
            if found:
                labels = found
                break

        for i in range(3):
            if i in labels:
                continue
            values = [v.get(f"option{i}") for v in variants]
            values = [v for v in values if v]
            if values:
                labels[i] = "talle" if looks_like_size(values) else "color"
        return labels

    @staticmethod
    def _breadcrumb(jsonld: dict) -> list[str]:
        items = (jsonld.get("breadcrumb") or {}).get("itemListElement") or []
        return [clean(i.get("name", "")) for i in items if i.get("name")]

    @staticmethod
    def _title(jsonld: dict, tree: HTMLParser) -> str:
        if name := (jsonld.get("_product") or {}).get("name"):
            return name
        if node := tree.css_first("h1"):
            return node.text()
        return ""

    @staticmethod
    def _description(jsonld: dict, tree: HTMLParser) -> str | None:
        """El JSON-LD trunca a ~150 chars con '...'. El HTML tiene el texto
        completo, que es donde viven las medidas y la composición de tela."""
        for sel in (".js-product-description", "[data-store='product-description']",
                    ".product-description"):
            if node := tree.css_first(sel):
                if txt := clean(node.text()):
                    return txt
        return clean((jsonld.get("_product") or {}).get("description")) or None

    @staticmethod
    def _brand(html: str) -> str | None:
        if m := _LSPRODUCT_RE.search(html):
            if bm := re.search(r"brand\s*:\s*'([^']*)'", m.group(1)):
                return clean(bm.group(1)) or None
        return None

    def _to_variant(self, v: dict, labels: dict[int, str]) -> Variant | None:
        vid = v.get("id")
        if vid is None:
            return None

        cents = parse_ars(v.get("price_number_raw") and v["price_number_raw"] / 100
                          or v.get("price_number"))
        if cents is None:
            return None
        compare = parse_ars(v.get("compare_at_price_number"))
        if compare is not None and compare <= cents:
            compare = None

        size = color = None
        extra: dict[str, str] = {}
        for i in range(3):
            value = v.get(f"option{i}")
            if not value:
                continue
            label = labels.get(i, "")
            if "talle" in label or "size" in label or "medida" in label:
                size = normalize_size(value)
            elif "color" in label:
                color = normalize_color(value)
            else:
                extra[label or f"option{i}"] = clean(value)

        stock = v.get("stock")
        img = v.get("image_url")
        return Variant(
            external_id=str(vid),
            sku=v.get("sku") or None,
            size=size,
            color=color,
            price=Price(amount_cents=cents, currency="ARS", compare_at_cents=compare),
            availability=(Availability.IN_STOCK if v.get("available")
                          else Availability.OUT_OF_STOCK),
            stock=stock if isinstance(stock, int) else None,
            image_url=f"https:{img}" if img and img.startswith("//") else img,
            extra_options=extra,
        )

    @staticmethod
    def _price_from_jsonld(jsonld: dict) -> Price | None:
        offers = ((jsonld.get("_product") or {}).get("offers")) or {}
        cents = parse_ars(offers.get("price"))
        return Price(amount_cents=cents) if cents else None

    @staticmethod
    def _images(html: str, tree: HTMLParser) -> list[Image]:
        """Solo la galería del producto de la ficha.

        Barrer el HTML entero con una regex trae también las fotos de los
        carruseles de recomendados: una gift card terminaba con nueve
        imágenes. Se acota al contenedor del detalle y, del CDN, se conserva
        el ancho mayor por slug para no guardar la misma foto cinco veces.
        """
        pattern = re.compile(
            r"(acdn[\w\-.]*\.mitiendanube\.com/stores/[\d/]+/products/"
            r"([\w\-]+?)-(\d+)-\d+\.(?:webp|jpg|jpeg|png))"
        )
        containers = tree.css(".js-product-detail-img-col") or tree.css(".js-product-detail")
        scope = "".join(c.html or "" for c in containers) or html

        best: dict[str, tuple[int, str, int]] = {}
        for order, (full, slug, w) in enumerate(pattern.findall(scope)):
            width = int(w)
            if slug not in best:
                best[slug] = (width, f"https://{full}", order)
            elif width > best[slug][0]:
                best[slug] = (width, f"https://{full}", best[slug][2])
        return [
            Image(url=url, position=i, width=width)
            for i, (width, url, _) in enumerate(sorted(best.values(), key=lambda t: t[2]))
        ][:12]

    @staticmethod
    def _distinct(values) -> list[str]:
        return list(dict.fromkeys(v for v in values if v))

    @staticmethod
    def _hash(variants: list[Variant], title: str) -> str:
        payload = title + "|" + "|".join(
            f"{v.external_id}:{v.price.amount_cents}:{v.availability}:{v.stock}"
            for v in sorted(variants, key=lambda x: x.external_id)
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
