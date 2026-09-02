"""Adapter Zara / Inditex.

Zara no corre ninguna de las plataformas conocidas: Inditex tiene la suya.
No hay `/products.json`, ni GraphQL, ni JSON-LD — la ficha de producto
devuelve 2 KB de cascarón con protección de bots. Pero el front del sitio
se alimenta de dos endpoints públicos y sin autenticación:

    /{pais}/{idioma}/categories?ajax=true          -> árbol de categorías
    /{pais}/{idioma}/category/{id}/products?ajax=true  -> hasta ~350 por llamada

Con eso alcanza para el catálogo completo a un costo bajísimo: una
categoría grande rinde 348 productos en un request.

Dos límites que conviene tener presentes:

- **No hay talles.** `detail.colors[].sizes` viene vacío en el listado, y
  los endpoints de detalle responden 403. Se registra una variante por
  color, con `size=None` — antes que inventar un talle que no publicaron.
- **Los precios vienen en centavos**, ya multiplicados: `28999000` es
  $289.990. Dividir de más da precios cien veces menores y pasa
  desapercibido porque siguen pareciendo plausibles.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..models import (
    Availability, Color, Gender, Image, Price, Product, Variant,
)
from ..normalize.color import normalize_color
from ..normalize.taxonomy import classify_category
from ..normalize.text import clean, normalize_title, slugify
from .base import StoreAdapter

# `sectionName` del propio Zara -> nuestro género
_SECCION = {
    "WOMAN": Gender.WOMEN, "MAN": Gender.MEN,
    "KID": Gender.KIDS, "KIDS": Gender.KIDS, "BEAUTY": Gender.UNKNOWN,
}
_DISPONIBLE = {
    "in_stock": Availability.IN_STOCK,
    "low_on_stock": Availability.IN_STOCK,
    "out_of_stock": Availability.OUT_OF_STOCK,
    "coming_soon": Availability.PREORDER,
}
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class ZaraAdapter(StoreAdapter):
    platform = "zara"

    def __init__(self, *a, pais: str = "ar", idioma: str = "es", **kw) -> None:
        super().__init__(*a, **kw)
        self.pais, self.idioma = pais, idioma
        self._base = f"https://{self.store.domain}/{pais}/{idioma}"

    async def _get(self, url: str) -> Any:
        async with httpx.AsyncClient(
            headers={"User-Agent": self.fetcher._client.headers["User-Agent"],
                     "Accept": "application/json"},
            timeout=TIMEOUT, follow_redirects=True,
        ) as c:
            try:
                r = await c.get(url)
            except httpx.HTTPError:
                return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------ descubrimiento
    async def categorias(self) -> list[dict]:
        """Hojas del árbol de categorías, aplanadas.

        El árbol trae ~830 nodos entre menús, separadores y agrupadores.
        Solo interesan los que tienen `id` y sección: el resto son títulos
        de navegación que no devuelven productos.
        """
        d = await self._get(f"{self._base}/categories?ajax=true")
        if not d:
            return []
        out: list[dict] = []

        def rec(nodos: list[dict]) -> None:
            for n in nodos or []:
                if n.get("id") and n.get("sectionName"):
                    out.append({"id": n["id"], "nombre": clean(n.get("name") or ""),
                                "seccion": n.get("sectionName")})
                rec(n.get("subcategories") or [])

        rec(d.get("categories") or [])
        # dedup por id conservando el primero (el de nombre más específico)
        vistos, unicas = set(), []
        for c in out:
            if c["id"] not in vistos:
                vistos.add(c["id"]); unicas.append(c)
        return unicas

    async def _productos_de(self, cat_id: int) -> list[dict]:
        d = await self._get(f"{self._base}/category/{cat_id}/products?ajax=true")
        if not isinstance(d, dict):
            return []
        return [c
                for g in (d.get("productGroups") or [])
                for e in (g.get("elements") or [])
                for c in (e.get("commercialComponents") or [])]

    async def discover_product_urls(self) -> list[str]:
        return [str(p.url) async for p in self.harvest()]

    async def harvest(self, limit: int | None = None) -> AsyncIterator[Product]:
        """Deduplica por REFERENCIA, no por id.

        Zara publica un `id` distinto por color del mismo modelo, y además
        repite el producto en cada categoría donde aparece. Deduplicar solo
        por id deja la grilla con el mismo vestido tres veces seguidas.
        La referencia (`03548246-S2026`) identifica el modelo; su parte
        antes del guion es estable entre colores y temporadas.
        """
        # Dos claves, no una: la referencia agrupa los colores de un modelo,
        # pero Zara a veces repite el mismo `id` bajo referencias distintas.
        # Filtrar solo por referencia deja pasar ids repetidos y la ingesta
        # muere con violación de unicidad a mitad del lote.
        refs: set[str] = set()
        ids: set[str] = set()
        n = 0
        for cat in await self.categorias():
            for raw in await self._productos_de(cat["id"]):
                pid = str(raw.get("id") or "")
                clave = self._clave(raw)
                if not pid or not clave or clave in refs or pid in ids:
                    continue
                refs.add(clave); ids.add(pid)
                if (prod := self.to_product(raw, cat)) is not None:
                    yield prod
                    n += 1
                    if limit and n >= limit:
                        return

    @staticmethod
    def _clave(raw: dict[str, Any]) -> str:
        """Modelo, no variante de color: `03548246-S2026` -> `03548246`."""
        ref = ((raw.get("detail") or {}).get("reference")
               or raw.get("reference") or "")
        if isinstance(ref, str) and ref:
            return ref.split("-")[0].strip()
        return str(raw.get("id") or "")

    # ------------------------------------------------------------------ mapeo
    def to_product(self, raw: dict[str, Any], cat: dict | None = None) -> Product | None:
        pid = raw.get("id")
        detalle = raw.get("detail") or {}
        cents = raw.get("price")
        if pid is None or not isinstance(cents, (int, float)) or cents <= 0:
            return None
        cents = int(cents)                       # ya viene en centavos
        lista = raw.get("oldPrice")
        lista = int(lista) if isinstance(lista, (int, float)) and lista > cents else None

        disp = _DISPONIBLE.get(str(raw.get("availability") or "").lower(),
                               Availability.UNKNOWN)
        colores = [c for c in (detalle.get("colors") or []) if isinstance(c, dict)]
        variants = [self._variante(c, pid, cents, lista, disp) for c in colores]
        variants = [v for v in variants if v]
        if not variants:
            # producto sin desglose de color: la ficha entera es una variante
            variants = [Variant(external_id=str(pid),
                                sku=clean(detalle.get("reference") or "") or None,
                                price=Price(amount_cents=cents, currency="ARS",
                                            compare_at_cents=lista),
                                availability=disp)]

        title = clean(raw.get("name") or "")
        if not title:
            return None
        seo = raw.get("seo") or {}
        handle = clean(seo.get("keyword") or "") or slugify(title)
        url = (f"{self._base}/{handle}-p{seo['seoProductId']}.html"
               if seo.get("seoProductId") else f"{self._base}/{handle}.html")

        # Zara clasifica mejor que nuestro diccionario: familyName/subfamilyName
        breadcrumb = [clean(x) for x in
                      (raw.get("familyName"), raw.get("subfamilyName"),
                       cat["nombre"] if cat else None) if clean(x or "")]

        _cat = classify_category(title, breadcrumb, handle)
        return Product(
            product_uid=f"{self.store.slug}:{pid}",
            store_slug=self.store.slug,
            external_id=str(pid),
            url=url,
            handle=handle,
            title=title,
            title_normalized=normalize_title(title),
            description=clean(raw.get("description") or "") or None,
            brand=self._marca(raw) or self.store.name,
            category_path=breadcrumb,
            category=_cat,
            gender=_SECCION.get(str(raw.get("sectionName") or
                                    (cat or {}).get("seccion") or "").upper(),
                                Gender.UNKNOWN),
            price_min=Price(amount_cents=cents, currency="ARS", compare_at_cents=lista),
            price_max=Price(amount_cents=cents, currency="ARS", compare_at_cents=lista),
            availability=disp,
            variants=variants,
            # sin talles: el listado no los trae y el detalle está bloqueado
            sizes_available=[],
            colors_available=self._distinct(
                v.color.raw for v in variants
                if v.color and v.availability is not Availability.OUT_OF_STOCK),
            images=self._imagenes(colores, raw),
            content_hash=hashlib.sha256(
                f"{title}|{cents}|{lista}|{disp.value}|{len(variants)}".encode()
            ).hexdigest()[:16],
            source="api",
            raw={k: v for k, v in raw.items() if k not in ("detail", "xmedia")},
        )

    @staticmethod
    def _marca(raw: dict[str, Any]) -> str | None:
        """`brand` puede venir como string o como objeto con `brandGroupCode`
        (Zara Home, Zara Kids). Asumir string rompe el parseo entero."""
        b = raw.get("brand")
        if isinstance(b, str):
            return clean(b) or None
        if isinstance(b, dict):
            for k in ("brandGroupDescription", "brandGroupCode", "name", "id"):
                if v := b.get(k):
                    return clean(str(v)) or None
        return None

    @staticmethod
    def _variante(c: dict, pid: Any, cents: int, lista: int | None,
                  disp: Availability) -> Variant | None:
        cid = c.get("id")
        nombre = clean(c.get("name") or "")
        color: Color | None = normalize_color(nombre) if nombre else None
        propio = c.get("price")
        precio = int(propio) if isinstance(propio, (int, float)) and propio > 0 else cents
        img = (c.get("xmedia") or [{}])[0]
        return Variant(
            external_id=f"{pid}-{cid}" if cid is not None else str(pid),
            sku=clean(c.get("reference") or "") or None,
            size=None,                       # ver docstring del módulo
            color=color,
            price=Price(amount_cents=precio, currency="ARS",
                        compare_at_cents=lista if precio == cents else None),
            availability=disp,
            stock=None,                      # Zara no publica cantidad
            image_url=ZaraAdapter._url_imagen(img),
        )

    @staticmethod
    def _url_imagen(m: dict) -> str | None:
        """El payload YA trae la URL armada.

        Construirla a mano desde `path` + `name` + `timestamp` da 404: el
        CDN espera el nombre repetido dentro de la carpeta. `url` y
        `extraInfo.deliveryUrl` vienen listos — usarlos en vez de adivinar.
        """
        if not isinstance(m, dict):
            return None
        for k in ("url",):
            if (u := m.get(k)) and isinstance(u, str) and u.startswith("http"):
                return u
        extra = m.get("extraInfo") or {}
        u = extra.get("deliveryUrl")
        return u if isinstance(u, str) and u.startswith("http") else None

    @classmethod
    def _imagenes(cls, colores: list[dict], raw: dict) -> list[Image]:
        urls: list[str] = []
        for c in colores:
            for m in (c.get("xmedia") or []):
                if u := cls._url_imagen(m):
                    urls.append(u)
        for m in (raw.get("xmedia") or []):
            if u := cls._url_imagen(m):
                urls.append(u)
        return [Image(url=u, position=i)
                for i, u in enumerate(dict.fromkeys(urls))][:12]

    @staticmethod
    def _distinct(values) -> list[str]:
        return list(dict.fromkeys(v for v in values if v))
