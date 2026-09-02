"""Talles: el campo más sucio de todo el retail argentino."""
from __future__ import annotations

import re

from ..models import Size, SizeSystem
from .text import clean, strip_accents

_ALPHA = {
    "xxs": "XXS", "xs": "XS", "s": "S", "small": "S",
    "m": "M", "medium": "M", "mediano": "M",
    "l": "L", "large": "L", "g": "L", "grande": "L",
    "xl": "XL", "xg": "XL", "xxl": "XXL", "xxg": "XXL", "xxxl": "XXXL",
}
_ONE_SIZE = {
    "unico", "talle unico", "u", "tu", "one size", "os",
    "unitalla", "único", "talle único",
}
_RANGE = re.compile(r"^(\d{1,2})\s*[-/aA]\s*(\d{1,2})$")
_DIM = re.compile(r"^(\d{2,3})\s*[xX×]\s*(\d{2,3})$")   # 40x40, 70x50 (medidas)


def normalize_size(raw: str | None) -> Size | None:
    """'Talle 38' -> 38/ar_numeric ; 'T.U' -> one_size ; 'l' -> L/alpha."""
    if raw is None:
        return None
    original = clean(raw)
    if not original:
        return None

    key = strip_accents(original).lower().strip(" .:-")
    key = re.sub(r"^(talle|talles|size|t)\b[\s.:-]*", "", key).strip()

    if key in _ONE_SIZE or strip_accents(original).lower() in _ONE_SIZE:
        return Size(raw=original, normalized="ONE SIZE", system=SizeSystem.ONE_SIZE)

    if key in _ALPHA:
        return Size(raw=original, normalized=_ALPHA[key], system=SizeSystem.ALPHA)

    if m := _RANGE.match(key):        # '38/40' -> se guarda el menor, sistema numérico
        return Size(raw=original, normalized=m.group(1), system=SizeSystem.AR_NUMERIC)

    if m := _DIM.match(key):          # '40x40' es una medida, no un color
        return Size(raw=original, normalized=f"{m.group(1)}x{m.group(2)}",
                    system=SizeSystem.DIMENSION)

    if key.isdigit():
        n = int(key)
        if 32 <= n <= 60:             # talles de indumentaria adulto AR
            return Size(raw=original, normalized=key, system=SizeSystem.AR_NUMERIC)
        if 20 <= n <= 31:             # calzado AR
            return Size(raw=original, normalized=key, system=SizeSystem.SHOE_AR)
        if 1 <= n <= 18:              # talles de niño por edad: 4, 6, 8, 10, 12...
            return Size(raw=original, normalized=key, system=SizeSystem.KIDS_NUMERIC)
        return Size(raw=original, normalized=key, system=SizeSystem.UNKNOWN)

    return Size(raw=original, normalized=None, system=SizeSystem.UNKNOWN)


def looks_like_size(values: list[str]) -> bool:
    """¿Esta lista de opciones son talles o colores?

    Solo se usa cuando el tema NO declara `<label for="variation_N">`, que
    resultó ser la mayoría de los temas. Una regla demasiado estrecha manda
    los talles al campo color sin avisar: con el rango 32-60 original, una
    tienda de ropa de nena (talles 4, 6, 8, 10, 12) guardaba TODOS sus
    talles como colores.
    """
    if not values:
        return False
    hits = sum(1 for v in values if (s := normalize_size(v)) and s.system != SizeSystem.UNKNOWN)
    return hits >= max(1, len(values) // 2)


def suspicious_color_set(values: list[str]) -> bool:
    """Alarma de calidad: un set de 'colores' todo numérico casi nunca es
    de colores. Marca la ficha para revisión en vez de contaminar el dataset."""
    vals = [v for v in values if v]
    if len(vals) < 2:
        return False
    return all(re.fullmatch(r"[\d\sxX×.,/-]+", v.strip()) for v in vals)
