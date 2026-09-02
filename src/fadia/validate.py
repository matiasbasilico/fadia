"""Arnés de validación: detectar que el parser se está confundiendo.

Esto no busca bugs en el código; busca **datasets que salieron mal sin que
nada fallara**. Los tres incidentes de este proyecto fueron todos así:

1. El JSON-LD de Tiendanube publicaba el precio de lista: precios 54 % más
   altos, cero excepciones.
2. Los talles de una tienda de ropa de nena (4, 6, 8, 10) se guardaron en
   el campo color: 27 variantes corruptas, cero excepciones.
3. Un `Accept-Encoding: br` sin brotli instalado devolvió bytes comprimidos
   como texto: 600 páginas HTTP 200, cero productos, el crawler informó
   "600/600, 0 errores".

Ninguno se habría detectado con try/except. Se detectan mirando la *forma*
del resultado y comparándola contra lo que ya sabemos del sitio.
"""
from __future__ import annotations

import re
import statistics as st
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from .models import Product
from .normalize.size import suspicious_color_set


class Severity(str, Enum):
    BLOCK = "block"    # no publicar este crawl
    WARN = "warn"      # publicar, pero avisar
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    message: str
    sample: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Baseline:
    """Lo que sabemos del sitio por corridas anteriores."""

    n_products: int | None = None
    median_price_cents: int | None = None
    coverage: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------- cobertura
# Piso de cobertura por campo. Debajo de esto, algo se rompió.
COVERAGE_FLOOR: dict[str, tuple[float, Severity]] = {
    "title":        (0.99, Severity.BLOCK),
    "price":        (0.95, Severity.BLOCK),
    "url":          (1.00, Severity.BLOCK),
    "images":       (0.80, Severity.WARN),
    "category":     (0.50, Severity.WARN),
    "description":  (0.40, Severity.INFO),
}

_GETTERS = {
    "title":       lambda p: bool(p.title and p.title.strip()),
    "price":       lambda p: p.price_min.amount_cents > 0,
    "url":         lambda p: bool(p.url),
    "images":      lambda p: bool(p.images),
    "category":    lambda p: bool(p.category),
    "description": lambda p: bool(p.description),
}

# Deriva tolerada contra la corrida anterior antes de sospechar
DRIFT_TOLERANCE = 0.30
PRICE_DRIFT_TOLERANCE = 3.0   # x3 o /3 en la mediana = casi seguro bug de unidad


def validate(
    products: Sequence[Product],
    *,
    baseline: Baseline | None = None,
    expected_urls: int | None = None,
) -> list[Finding]:
    """Corre todos los chequeos y devuelve los hallazgos, peor primero."""
    out: list[Finding] = []
    if not products:
        return [Finding("vacio", Severity.BLOCK, "El crawl no produjo un solo producto.")]

    out += _check_coverage(products)
    out += _check_option_semantics(products)
    out += _check_prices(products)
    out += _check_identity(products)
    out += _check_content(products)
    if baseline:
        out += _check_drift(products, baseline)
    if expected_urls:
        out += _check_completeness(products, expected_urls)

    order = {Severity.BLOCK: 0, Severity.WARN: 1, Severity.INFO: 2}
    return sorted(out, key=lambda f: order[f.severity])


# ---------------------------------------------------------------- chequeos
def _check_coverage(ps: Sequence[Product]) -> list[Finding]:
    n = len(ps)
    out = []
    for field_name, (floor, sev) in COVERAGE_FLOOR.items():
        got = sum(1 for p in ps if _GETTERS[field_name](p)) / n
        if got < floor:
            out.append(Finding(
                f"cobertura:{field_name}", sev,
                f"{field_name} presente en {got:.1%} (piso {floor:.0%}) sobre {n} fichas.",
            ))
    return out


def _check_option_semantics(ps: Sequence[Product]) -> list[Finding]:
    """El incidente de los talles de nena.

    Un conjunto de 'colores' enteramente numérico no es de colores: es un
    talle mal etiquetado. Se mide por producto, no global, porque una sola
    tienda puede contaminar un lote entero.
    """
    culpables = []
    for p in ps:
        colores = [v.color.raw for v in p.variants if v.color]
        if len(colores) >= 2 and suspicious_color_set(colores):
            culpables.append(f"{p.product_uid} {colores[:4]}")
    if not culpables:
        return []
    ratio = len(culpables) / len(ps)
    sev = Severity.BLOCK if ratio > 0.05 else Severity.WARN
    return [Finding(
        "semantica:color-numerico", sev,
        f"{len(culpables)} productos ({ratio:.1%}) tienen colores todo-numéricos: "
        f"casi seguro son talles en el campo equivocado.",
        culpables[:5],
    )]


def _check_prices(ps: Sequence[Product]) -> list[Finding]:
    out = []
    cents = [p.price_min.amount_cents for p in ps if p.price_min.amount_cents > 0]
    if not cents:
        return [Finding("precio:vacio", Severity.BLOCK, "Ningún producto tiene precio.")]

    # precios redondos de más: síntoma de haber leído el precio de lista
    redondos = sum(1 for c in cents if c % 100000 == 0) / len(cents)
    if redondos > 0.60:
        out.append(Finding(
            "precio:demasiado-redondo", Severity.WARN,
            f"{redondos:.0%} de los precios son múltiplos exactos de $1.000. "
            f"Revisar si se está leyendo el precio de lista y no el de venta.",
        ))

    # compare_at por debajo del precio: descuento invertido
    invertidos = [p.product_uid for p in ps
                  if p.price_min.compare_at_cents
                  and p.price_min.compare_at_cents <= p.price_min.amount_cents]
    if invertidos:
        out.append(Finding(
            "precio:descuento-invertido", Severity.WARN,
            f"{len(invertidos)} productos con precio tachado menor o igual al de venta.",
            invertidos[:5],
        ))

    # outliers absurdos: un x100 aislado es error de unidad
    if len(cents) > 20:
        mediana = st.median(cents)
        locos = [p.product_uid for p in ps
                 if p.price_min.amount_cents > mediana * 500
                 or (0 < p.price_min.amount_cents < mediana / 500)]
        if locos:
            out.append(Finding(
                "precio:outlier", Severity.WARN,
                f"{len(locos)} precios a más de 500x de la mediana "
                f"(${mediana/100:,.0f}): posible error de unidad.",
                locos[:5],
            ))
    return out


def _check_identity(ps: Sequence[Product]) -> list[Finding]:
    out = []
    uids = [p.product_uid for p in ps]
    dupes = len(uids) - len(set(uids))
    if dupes:
        out.append(Finding(
            "identidad:duplicados", Severity.BLOCK,
            f"{dupes} product_uid repetidos: el upsert va a pisar datos.",
        ))
    # títulos idénticos en masa = se está leyendo el producto equivocado
    titulos = [p.title_normalized for p in ps if p.title_normalized]
    if titulos and len(set(titulos)) / len(titulos) < 0.30:
        out.append(Finding(
            "identidad:titulos-repetidos", Severity.BLOCK,
            f"Solo {len(set(titulos))} títulos distintos en {len(titulos)} fichas: "
            f"probablemente se está parseando siempre el mismo bloque.",
        ))
    return out


def _check_content(ps: Sequence[Product]) -> list[Finding]:
    """El incidente del brotli: HTTP 200 y texto ilegible.

    Si el HTML no se decodificó, los títulos salen con caracteres de control
    o mojibake. Es barato de detectar y evita publicar un lote entero de basura.
    """
    basura = [p.product_uid for p in ps
              if p.title and (re.search(r"[\x00-\x08\x0e-\x1f]", p.title)
                              or p.title.count("�") > 1)]
    if basura:
        ratio = len(basura) / len(ps)
        return [Finding(
            "contenido:no-decodificado", Severity.BLOCK,
            f"{len(basura)} títulos ({ratio:.1%}) con caracteres de control o "
            f"reemplazo: el HTML probablemente no se descomprimió.",
            basura[:5],
        )]
    return []


def _check_drift(ps: Sequence[Product], base: Baseline) -> list[Finding]:
    out = []
    if base.n_products:
        delta = abs(len(ps) - base.n_products) / base.n_products
        if delta > DRIFT_TOLERANCE:
            out.append(Finding(
                "deriva:cantidad", Severity.WARN,
                f"{len(ps)} productos vs {base.n_products} de la corrida anterior "
                f"({delta:+.0%}). O el catálogo cambió mucho, o el crawl quedó corto.",
            ))
    if base.median_price_cents:
        cents = [p.price_min.amount_cents for p in ps if p.price_min.amount_cents > 0]
        if cents:
            ratio = st.median(cents) / base.median_price_cents
            if ratio > PRICE_DRIFT_TOLERANCE or ratio < 1 / PRICE_DRIFT_TOLERANCE:
                out.append(Finding(
                    "deriva:precio", Severity.BLOCK,
                    f"Mediana de precio {ratio:.1f}x la anterior "
                    f"(${st.median(cents)/100:,.0f} vs ${base.median_price_cents/100:,.0f}). "
                    f"Un salto así casi siempre es de unidad, no de mercado.",
                ))
    for field_name, antes in base.coverage.items():
        if field_name not in _GETTERS:
            continue
        ahora = sum(1 for p in ps if _GETTERS[field_name](p)) / len(ps)
        if antes - ahora > 0.20:
            out.append(Finding(
                f"deriva:cobertura:{field_name}", Severity.WARN,
                f"{field_name} bajó de {antes:.0%} a {ahora:.0%}: "
                f"el sitio probablemente cambió de template.",
            ))
    return out


def _check_completeness(ps: Sequence[Product], expected: int) -> list[Finding]:
    got = len(ps) / expected
    if got >= 0.95:
        return []
    sev = Severity.BLOCK if got < 0.80 else Severity.WARN
    return [Finding(
        "completitud", sev,
        f"Se extrajeron {len(ps)} de {expected} fichas esperadas ({got:.1%}).",
    )]


def coverage_report(ps: Sequence[Product]) -> dict[str, float]:
    """Para guardar como baseline de la próxima corrida."""
    n = len(ps) or 1
    return {k: sum(1 for p in ps if g(p)) / n for k, g in _GETTERS.items()}
