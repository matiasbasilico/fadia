"""Colores -> familia canónica. Las tiendas AR inventan nombres sin límite
('Off white', 'Butter yellow', 'Crudo', 'Nude'); el buscador necesita
agruparlos en ~15 familias."""
from __future__ import annotations

import re

from ..models import Color
from .text import clean, strip_accents

# familia canónica -> tokens que la disparan (sin acentos, minúscula)
_FAMILIES: dict[str, tuple[str, ...]] = {
    "black":  ("negro", "black", "azabache"),
    "white":  ("blanco", "white", "off white", "offwhite", "crudo", "marfil", "hueso", "ivory", "crema", "champagne", "champan"),
    "grey":   ("gris", "grey", "gray", "plomo", "melange", "plata", "silver", "plateado"),
    "beige":  ("beige", "arena", "nude", "camel", "tostado", "taupe", "tiza", "khaki", "kaki"),
    "brown":  ("marron", "brown", "chocolate", "cafe", "tabaco", "cuero", "topo", "choco"),
    "red":    ("rojo", "red", "bordo", "burdeos", "vino", "granate", "teja", "borravino", "cherry", "cereza"),
    "pink":   ("rosa", "rosado", "pink", "fucsia", "fuchsia", "coral", "salmon", "palo de rosa"),
    "orange": ("naranja", "orange", "mandarina", "ladrillo", "oxido"),
    "yellow": ("amarillo", "yellow", "mostaza", "butter", "manteca", "dorado", "gold", "oro"),
    "green":  ("verde", "green", "oliva", "militar", "menta", "lima", "esmeralda", "petroleo"),
    "blue":   ("azul", "blue", "celeste", "navy", "marino", "denim", "jean", "aqua", "turquesa"),
    "purple": ("violeta", "lila", "purple", "morado", "malva", "uva"),
    "multi":  ("multicolor", "estampado", "print", "animal print", "rayado", "floral", "tie dye"),
}

_TOKEN_TO_FAMILY: list[tuple[str, str]] = sorted(
    ((tok, fam) for fam, toks in _FAMILIES.items() for tok in toks),
    key=lambda p: -len(p[0]),          # match más largo primero: 'off white' antes que 'white'
)

_HEX = {
    "black": "#000000", "white": "#FFFFFF", "grey": "#8A8A8A", "beige": "#D8C3A5",
    "brown": "#6F4E37", "red": "#C0392B", "pink": "#E8A0BF", "orange": "#E67E22",
    "yellow": "#F1C40F", "green": "#27AE60", "blue": "#2C6FBB", "purple": "#8E44AD",
}


def normalize_color(raw: str | None) -> Color | None:
    if raw is None:
        return None
    original = clean(raw)
    if not original:
        return None

    key = re.sub(r"[^a-z0-9\s]+", " ", strip_accents(original).lower()).strip()
    for token, family in _TOKEN_TO_FAMILY:
        if re.search(rf"\b{re.escape(token)}\b", key):
            return Color(raw=original, normalized=family, hex=_HEX.get(family))
    return Color(raw=original, normalized=None)
