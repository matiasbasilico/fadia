"""Categoría canónica + género.

Las tiendas mezclan taxonomía real ('vestidos') con colecciones de
marketing ('new-in', '2x1'). Las separamos: la taxonomía sirve para
filtrar, la colección solo para merchandising.
"""
from __future__ import annotations

import re

from ..models import Gender
from .text import strip_accents

# categoría canónica -> patrones (se evalúa en orden)
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("dresses",     ("vestido", "dress", "enterito", "mono ", "jumpsuit")),
    ("jeans",       ("jean", "denim", "mom fit", "wide leg")),
    ("pants",       ("pantalon", "pants", "pant", "cargo", "jogger", "calza", "legging",
                     "babucha", "palazzo", "sastrero")),
    ("skirts",      ("pollera", "skirt", "falda", "faldas")),
    ("shorts",      ("short", "bermuda")),
    ("tops",        ("top", "remera", "reme", "chomba", "musculosa", "blusa", "camisa",
                     "body", "corset", "shirt", "tee", "crop",
                     "camiseta", "polo")),                       # peninsular
    ("knitwear",    ("sweater", "buzo", "hoodie", "cardigan", "pullover", "sudadera",
                     "jersey", "punto", "chaleco")),               # peninsular
    ("outerwear",   ("campera", "abrigo", "tapado", "saco", "blazer", "trench", "parka",
                     "chaqueta", "cazadora", "gabardina", "abrigos", "plumifero",
                     "plumífero", "anorak")),                     # peninsular
    ("swimwear",    ("bikini", "malla", "swim", "salida de bano")),
    ("lingerie",    ("lenceria", "conjunto de ropa interior", "corpino", "bombacha", "pijama", "night")),
    ("activewear",  ("deportivo", "active", "sport", "training")),
    ("shoes",       ("zapatilla", "zapato", "calzado", "bota", "sandalia", "sandal",
                     "borcego", "mocasin", "shoes", "footwear", "botineta", "alpargata")),
    ("bags",        ("cartera", "bolso", "bolsos", "mochila", "riñonera", "rinonera",
                     "bag", "bandolera", "shopper")),
    ("jewelry",     ("collar", "anillo", "aros", "pulsera", "bracelet", "necklace", "earring")),
    ("accessories", ("accesorio", "cinto", "gorra", "bufanda", "pañuelo", "panuelo",
                     "lentes", "medias", "gorro", "cinturon", "cinturón", "guantes",
                     "sombrero", "pashmina")),
    ("giftcard",    ("gift card", "giftcard", "tarjeta de regalo")),
]

# rutas que son campañas, no taxonomía
_COLLECTION_HINTS = (
    "new-in", "newin", "sale", "outlet", "2x1", "3x2", "promo", "descuento",
    "black-friday", "cyber", "hot-sale", "destacado", "lo-mas", "days", "essentials",
    "verano", "invierno", "primavera", "otono", "capsula", "drop", "preventa",
)

_GENDER_RULES: list[tuple[Gender, tuple[str, ...]]] = [
    (Gender.KIDS,  ("nino", "nina", "kids", "infantil", "bebe", "junior")),
    (Gender.MEN,   ("hombre", "men", "masculino", "caballero")),
    (Gender.WOMEN, ("mujer", "women", "femenino", "dama", "womens")),
]


def _hay(*parts: str | None) -> str:
    return strip_accents(" ".join(p for p in parts if p)).lower()


def classify_category(title: str, breadcrumb: list[str], handle: str = "") -> str | None:
    """El breadcrumb pesa más que el título: es la intención de la tienda."""
    for source in (_hay(*breadcrumb), _hay(title, handle)):
        for category, patterns in _CATEGORY_RULES:
            # Palabra completa (con plural opcional), no prefijo. Con match de
            # prefijo, "calza" —la prenda— capturaba "calzado" y todas las
            # zapatillas terminaban clasificadas como pantalones.
            if any(re.search(rf"\b{re.escape(p)}s?\b", source) for p in patterns):
                return category
    return None


def is_collection(path_segment: str) -> bool:
    seg = strip_accents(path_segment).lower()
    return any(h in seg for h in _COLLECTION_HINTS)


# Categorías que en la práctica son casi exclusivamente de mujer. No es un
# juicio sobre quién puede usar qué: es que en los catálogos argentinos
# relevados, estas categorías vienen del lado femenino salvo excepción.
_CATEGORIAS_MUJER = {"dresses", "skirts", "lingerie"}


def gender_por_categoria(category: str | None) -> Gender:
    return Gender.WOMEN if category in _CATEGORIAS_MUJER else Gender.UNKNOWN


def classify_gender(title: str, breadcrumb: list[str], store_default: Gender = Gender.UNKNOWN,
                    category: str | None = None) -> Gender:
    """Match por palabra completa, no por substring.

    Con `in` a secas, 'REME XIAMEN' cae en men y 'CAMISA' en nino: el
    dataset se contamina con géneros inventados que después filtran mal.
    """
    source = _hay(title, *breadcrumb)
    for gender, patterns in _GENDER_RULES:
        if any(re.search(rf"\b{re.escape(p)}s?\b", source) for p in patterns):
            return gender
    if (g := gender_por_categoria(category)) is not Gender.UNKNOWN:
        return g
    return store_default
