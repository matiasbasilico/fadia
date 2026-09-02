"""Limpieza de texto: la base de todo lo demás."""
from __future__ import annotations

import re
import unicodedata

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF️❤]+"
)
_WS = re.compile(r"\s+")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def clean(s: str | None) -> str:
    """Colapsa espacios, saca emojis y caracteres de control."""
    if not s:
        return ""
    s = _EMOJI.sub(" ", s)
    s = s.replace("\xa0", " ")
    return _WS.sub(" ", s).strip()


def slugify(s: str) -> str:
    s = strip_accents(clean(s)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def normalize_title(title: str) -> str:
    """Título comparable entre tiendas: sin acentos, sin ruido, minúsculas.

    'DRESS JAMIE 💌' -> 'dress jamie'
    """
    t = strip_accents(clean(title)).lower()
    t = re.sub(r"[^a-z0-9\s]+", " ", t)
    return _WS.sub(" ", t).strip()
