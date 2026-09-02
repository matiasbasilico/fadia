"""Precios argentinos. Todo a centavos enteros."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_CLEAN = re.compile(r"[^\d,.\-]")


def parse_ars(value: str | int | float | None) -> int | None:
    """Parsea '$36.500,00' / '36500' / 36500.0 -> centavos.

    Formato AR: punto = miles, coma = decimales. Es el inverso del
    formato US, así que un parser ingenuo devuelve 36.5 en vez de 36500.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(round(Decimal(str(value)) * 100))

    s = _CLEAN.sub("", str(value)).strip()
    if not s:
        return None

    if "," in s:                      # coma = decimal → punto es separador de miles
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") == 1:
        entera, dec = s.split(".")
        if len(dec) == 3 and len(entera) <= 3:   # '36.500' = treinta y seis mil
            s = entera + dec
    else:
        s = s.replace(".", "")

    try:
        return int(round(Decimal(s) * 100))
    except (InvalidOperation, ValueError):
        return None
