"""Opciones con nombre -> talle / color.

Shopify, VTEX y Magento **nombran** sus opciones ("Color", "Talle",
"Size"), a diferencia de Tiendanube donde son posicionales y hay que
inferirlas. Cuando el nombre está, se usa; solo si no se reconoce se cae
a la inferencia por valores.
"""
from __future__ import annotations

import re

from ..models import Color, Size
from ..normalize.color import normalize_color
from ..normalize.size import looks_like_size, normalize_size
from ..normalize.text import clean, strip_accents

_ES_TALLE = re.compile(r"talle|size|medida|numero|número|n°|calce", re.I)
_ES_COLOR = re.compile(r"color|colour|tono", re.I)


def clasificar(nombre: str) -> str:
    """'Talle' -> 'size', 'Color' -> 'color', otro -> 'extra'."""
    n = strip_accents(clean(nombre)).lower()
    if _ES_TALLE.search(n):
        return "size"
    if _ES_COLOR.search(n):
        return "color"
    return "extra"


def repartir(
    opciones: list[tuple[str, str]],
) -> tuple[Size | None, Color | None, dict[str, str]]:
    """`[('Color','Rosa'), ('Talle','35')]` -> (Size, Color, extras).

    Si un nombre no se reconoce, se decide por el valor: es la misma
    inferencia que salvó a las tiendas Tiendanube sin `<label>`.
    """
    size = color = None
    extras: dict[str, str] = {}
    sin_clasificar: list[tuple[str, str]] = []

    for nombre, valor in opciones:
        if not valor:
            continue
        tipo = clasificar(nombre)
        if tipo == "size" and size is None:
            size = normalize_size(valor)
        elif tipo == "color" and color is None:
            color = normalize_color(valor)
        else:
            sin_clasificar.append((nombre, valor))

    for nombre, valor in sin_clasificar:
        if size is None and looks_like_size([valor]):
            size = normalize_size(valor)
        elif color is None and not looks_like_size([valor]):
            color = normalize_color(valor)
        else:
            extras[clean(nombre) or "opcion"] = clean(valor)
    return size, color, extras
