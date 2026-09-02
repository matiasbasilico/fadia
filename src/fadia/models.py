"""Esquema canónico: la única forma en que el resto del sistema ve un producto.

Cada tienda entra por un adapter distinto y sale siempre con esta forma.
`raw` conserva el payload original sin tocar para poder re-normalizar
en el futuro sin volver a scrapear.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class Availability(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    UNKNOWN = "unknown"


class Gender(str, Enum):
    WOMEN = "women"
    MEN = "men"
    UNISEX = "unisex"
    KIDS = "kids"
    UNKNOWN = "unknown"


class SizeSystem(str, Enum):
    ALPHA = "alpha"          # XS, S, M, L, XL
    AR_NUMERIC = "ar_numeric"  # 34, 36, 38, 40...
    SHOE_AR = "shoe_ar"      # 35, 36, 37...
    KIDS_NUMERIC = "kids_numeric"  # 2, 4, 6, 8... talle por edad
    DIMENSION = "dimension"  # 40x40, 70x50 (blanquería, deco)
    ONE_SIZE = "one_size"
    UNKNOWN = "unknown"


class Price(BaseModel):
    """Dinero en centavos enteros. Nunca float: ARS con inflación y
    descuentos encadenados acumula error de redondeo rápido."""

    amount_cents: int
    currency: str = "ARS"
    compare_at_cents: int | None = None  # precio tachado / de lista

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_cents) / 100

    @property
    def discount_pct(self) -> float | None:
        if not self.compare_at_cents or self.compare_at_cents <= self.amount_cents:
            return None
        return round(100 * (1 - self.amount_cents / self.compare_at_cents), 2)


class Size(BaseModel):
    """El talle tal como lo publica la tienda + su lectura normalizada."""

    raw: str
    normalized: str | None = None
    system: SizeSystem = SizeSystem.UNKNOWN


class Color(BaseModel):
    raw: str
    normalized: str | None = None   # familia de color canónica
    hex: str | None = None


class Variant(BaseModel):
    """Un SKU comprable concreto. Es el nivel donde vive precio y stock."""

    external_id: str                 # id de variante en la tienda
    sku: str | None = None
    size: Size | None = None
    color: Color | None = None
    price: Price
    availability: Availability = Availability.UNKNOWN
    stock: int | None = None
    image_url: HttpUrl | None = None
    extra_options: dict[str, str] = Field(default_factory=dict)


class Image(BaseModel):
    url: HttpUrl
    position: int = 0
    width: int | None = None
    height: int | None = None


class Seller(BaseModel):
    """En un marketplace el vendedor no es el sitio: Avellaneda a un Toque
    publica 3.856 locales distintos. Sin esto, todo el catálogo colapsa en
    una sola 'marca' y se pierde a quién comprarle."""

    external_id: str | None = None
    name: str
    address: str | None = None
    verified: bool = False
    followers: int | None = None


class Store(BaseModel):
    slug: str                        # "sunnyclothing"
    name: str
    domain: str
    platform: str                    # "tiendanube" | "shopify" | ...
    country: str = "AR"


class Product(BaseModel):
    """Documento canónico. Un producto = N variantes."""

    # --- identidad ---
    product_uid: str                 # "{store_slug}:{external_id}" — clave de upsert
    store_slug: str
    external_id: str
    url: HttpUrl
    handle: str                      # slug dentro de la tienda

    # --- contenido ---
    title: str
    title_normalized: str            # sin ruido de marca/emoji, para matching
    description: str | None = None
    brand: str | None = None

    # --- clasificación ---
    category_path: list[str] = Field(default_factory=list)  # breadcrumb crudo
    category: str | None = None      # categoría canónica ("dresses")
    gender: Gender = Gender.UNKNOWN
    collections: list[str] = Field(default_factory=list)    # "new-in", "2x1" (marketing)
    tags: list[str] = Field(default_factory=list)

    # --- comercial (agregados sobre las variantes) ---
    price_min: Price
    price_max: Price
    price_retail: Price | None = None   # 'precio por menor' en mayoristas
    min_purchase: str | None = None     # texto libre tal cual lo escribió el local
    min_qty: int | None = None          # parseado cuando se puede; None si no
    min_unit: str | None = None         # 'unidades' | 'pares' | 'curva' | ...
    seller: Seller | None = None
    availability: Availability = Availability.UNKNOWN
    variants: list[Variant] = Field(default_factory=list)
    sizes_available: list[str] = Field(default_factory=list)
    colors_available: list[str] = Field(default_factory=list)

    # --- media ---
    images: list[Image] = Field(default_factory=list)

    # --- procedencia ---
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str | None = None  # detecta cambios sin difear todo el doc
    source: str = "html"             # html | jsonld | api
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class PricePoint(BaseModel):
    """Serie temporal separada del producto: append-only, nunca se pisa.
    Es lo que habilita 'bajó de precio' y 'histórico de esta prenda'."""

    product_uid: str
    variant_external_id: str
    observed_at: datetime
    amount_cents: int
    compare_at_cents: int | None = None
    availability: Availability
    stock: int | None = None
