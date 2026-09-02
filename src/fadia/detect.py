"""Detección de plataforma. Define qué adapter usar.

En Argentina el reparto es muy concentrado: Tiendanube domina indumentaria
independiente, después VTEX (cadenas grandes) y Shopify. Escribís un
adapter por plataforma, no por tienda: cubrís cientos de sitios con cinco.
"""
from __future__ import annotations

import re

# Plataformas de UN SOLO INQUILINO: se reconocen por dominio, no por HTML.
# La home de zara.com devuelve 2 KB sin una sola mención a "zara" —
# protección anti-bot— así que buscar firmas en el contenido nunca acierta.
_POR_HOST: list[tuple[str, re.Pattern[str]]] = [
    ("zara", re.compile(r"(^|\.)zara\.com$", re.I)),
    ("avellaneda", re.compile(r"avellanedaa(?:un|l)toque\.com$", re.I)),
]

_SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("avellaneda", re.compile(r"avellanedaa(?:un|l)toque\.com", re.I)),
    ("tiendanube", re.compile(r"tiendanube|nuvemshop|mitiendanube\.com", re.I)),
    ("shopify",    re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.I)),
    ("vtex",       re.compile(r"vtexassets\.com|vtexcommercestable", re.I)),
    ("woocommerce", re.compile(r"woocommerce|wp-content/plugins/woo", re.I)),
    ("empretienda", re.compile(r"empretienda", re.I)),
    ("magento",    re.compile(r"Magento_|mage/cookies", re.I)),
]


def detect_platform(html: str, domain: str | None = None) -> str:
    """Slug de plataforma, o 'unknown'.

    Primero por dominio (plataformas de un solo inquilino), después por
    firmas en el HTML contando ocurrencias — una mención suelta dentro de
    un script no alcanza para decidir.
    """
    if domain:
        host = domain.lower().strip().removeprefix("www.")
        for nombre, pat in _POR_HOST:
            if pat.search(host) or pat.search(domain.lower()):
                return nombre
    scores = {name: len(pat.findall(html)) for name, pat in _SIGNATURES}
    best, hits = max(scores.items(), key=lambda kv: kv[1])
    return best if hits >= 3 else "unknown"


# ---------------------------------------------------------------- por endpoint
# Olfatear el HTML falla cuando la home es un cascarón, está detrás de un
# CDN o el tema no nombra la plataforma: Tucci corre Magento y su markup no
# lo dice en ningún lado. Pedirle la API directamente es prueba dura y sin
# falsos positivos. Cuesta hasta 4 requests y solo se usa cuando el HTML no
# alcanzó.

_SONDAS: tuple[tuple[str, str, str, dict], ...] = (
    ("shopify", "GET", "/products.json?limit=1", {}),
    ("vtex", "GET", "/api/catalog_system/pub/products/search?_from=0&_to=0", {}),
    ("magento", "POST", "/graphql",
     {"json": {"query": "{products(search:\"\" pageSize:1 currentPage:1)"
                        "{total_count items{sku}}}"},
      "headers": {"Content-Type": "application/json"}}),
)


async def detect_by_endpoint(domain: str, client) -> str:
    """Slug de plataforma probando sus APIs. 'unknown' si ninguna responde."""
    import json as _json

    for nombre, metodo, ruta, kw in _SONDAS:
        try:
            r = await (client.post(f"https://{domain}{ruta}", timeout=25.0, **kw)
                       if metodo == "POST"
                       else client.get(f"https://{domain}{ruta}", timeout=25.0, **kw))
        except Exception:                                # noqa: BLE001
            continue
        if r.status_code not in (200, 206):
            continue
        if "json" not in r.headers.get("content-type", ""):
            continue
        try:
            d = r.json()
        except _json.JSONDecodeError:
            continue
        if nombre == "shopify" and isinstance(d, dict) and isinstance(d.get("products"), list):
            return "shopify"
        if nombre == "vtex" and isinstance(d, list):
            return "vtex"
        if nombre == "magento" and isinstance(d, dict) and (d.get("data") or {}).get("products"):
            return "magento"

    # Tiendanube no tiene API pública, pero su sitemap es inconfundible
    try:
        r = await client.get(f"https://{domain}/sitemap.xml", timeout=25.0)
        if r.status_code == 200 and "/productos/" in r.text:
            return "tiendanube"
    except Exception:                                    # noqa: BLE001
        pass
    return "unknown"
