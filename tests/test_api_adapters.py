"""Regresiones de los adapters de API (Shopify, VTEX, Magento).

Corren sobre payloads congelados: fallan en CI cuando alguien rompe el
mapeo, sin depender de que la tienda siga online.
"""
import json
from pathlib import Path

import pytest

from fadia.adapters.magento import MagentoAdapter
from fadia.adapters.shopify import ShopifyAdapter
from fadia.adapters.vtex import VtexAdapter
from fadia.models import Availability, SizeSystem, Store

FIXTURES = Path(__file__).parent / "fixtures"


def _raw(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _adapter(cls, slug: str):
    return cls(Store(slug=slug, name=slug, domain=f"{slug}.com", platform=cls.platform),
               fetcher=None)


# ---------------------------------------------------------------- shopify
def test_shopify_mapea_opciones_por_nombre():
    """Shopify nombra las opciones: no hay que adivinar posiciones."""
    p = _adapter(ShopifyAdapter, "eyelit").to_product(_raw("shopify_eyelit.json"))
    assert p is not None
    assert p.variants, "debe haber variantes"
    v = p.variants[0]
    assert v.color is not None and v.color.raw, "option Color -> color"
    assert v.size is not None and v.size.raw, "option Talle -> talle"


def test_shopify_compare_at_cero_no_es_precio_de_lista():
    """Shopify manda '0.00' cuando NO hay descuento, no null.

    Tomarlo literal produce descuentos del 100 % sobre todo el catálogo.
    """
    raw = _raw("shopify_eyelit.json")
    for v in raw["variants"]:
        v["compare_at_price"] = "0.00"
    p = _adapter(ShopifyAdapter, "eyelit").to_product(raw)
    assert all(v.price.compare_at_cents is None for v in p.variants)
    assert p.price_min.discount_pct is None


def test_shopify_no_inventa_stock():
    """products.json no publica cantidad: None, nunca 0."""
    p = _adapter(ShopifyAdapter, "eyelit").to_product(_raw("shopify_eyelit.json"))
    assert all(v.stock is None for v in p.variants)
    assert any(v.availability is not Availability.UNKNOWN for v in p.variants)


# ---------------------------------------------------------------- vtex
def test_vtex_mapea_variations_y_stock_real():
    p = _adapter(VtexAdapter, "47street").to_product(_raw("vtex_47street.json"))
    assert p is not None
    v = p.variants[0]
    assert v.size is not None, "'Talle' de variations -> talle"
    assert v.color is not None, "'Color' de variations -> color"
    assert isinstance(v.stock, int), "VTEX sí publica AvailableQuantity"
    assert v.price.amount_cents > 0


def test_vtex_categorias_del_path():
    p = _adapter(VtexAdapter, "47street").to_product(_raw("vtex_47street.json"))
    assert p.category_path, "'/Calzado/Zapatillas/' -> ['Calzado','Zapatillas']"
    assert "/" not in "".join(p.category_path)


def test_vtex_listprice_igual_al_precio_no_es_descuento():
    raw = _raw("vtex_47street.json")
    for it in raw["items"]:
        of = it["sellers"][0]["commertialOffer"]
        of["ListPrice"] = of["Price"]
    p = _adapter(VtexAdapter, "47street").to_product(raw)
    assert all(v.price.compare_at_cents is None for v in p.variants)


# ---------------------------------------------------------------- magento
def test_magento_esquema_viejo_sin_uid():
    """bowen.com.ar corre Magento < 2.4.2: el payload no trae `uid`."""
    raw = _raw("magento_bowen.json")
    assert "uid" not in raw, "fixture de una versión vieja, a propósito"
    p = _adapter(MagentoAdapter, "bowen").to_product(raw)
    assert p is not None and p.variants


def test_magento_variante_hereda_precio_del_padre():
    raw = _raw("magento_bowen.json")
    for v in raw.get("variants", []):
        v["product"]["price_range"] = None
    p = _adapter(MagentoAdapter, "bowen").to_product(raw)
    assert p is not None
    assert all(v.price.amount_cents > 0 for v in p.variants)


def test_magento_tolera_nulls_en_las_listas():
    """GraphQL mete null adentro de las listas y rompe el parseo a mitad."""
    raw = _raw("magento_bowen.json")
    raw["categories"] = [None] + (raw.get("categories") or [])
    raw["media_gallery"] = [None] + (raw.get("media_gallery") or [])
    for v in raw.get("variants", []):
        v["attributes"] = [None] + (v.get("attributes") or [])
    p = _adapter(MagentoAdapter, "bowen").to_product(raw)
    assert p is not None


def test_magento_descripcion_cae_a_description():
    """`short_description` suele venir vacío; el texto está en `description`."""
    raw = _raw("magento_bowen.json")
    raw["short_description"] = {"html": ""}
    raw["description"] = {"html": "<p>Mocasín de cuero</p>"}
    p = _adapter(MagentoAdapter, "bowen").to_product(raw)
    assert p.description and "Mocas" in p.description


# ---------------------------------------------------------------- taxonomía
@pytest.mark.parametrize("titulo,breadcrumb,esperado", [
    ("ZAPATILLA KITTY BALLOON H.", ["Calzado", "Zapatillas"], "shoes"),
    ("MOCASIN SMART", ["BOTAS Y ZAPATOS"], "shoes"),
    ("Crocs Soho Y-Strap Sandal", ["Shoes"], "shoes"),
    ("Calza deportiva", ["Mujer"], "pants"),
    ("REME ANABELLA", [], "tops"),
])
def test_calzado_no_es_pantalon(titulo, breadcrumb, esperado):
    """'calza' con match de prefijo capturaba 'calzado': todas las
    zapatillas quedaban clasificadas como pantalones."""
    from fadia.normalize.taxonomy import classify_category
    assert classify_category(titulo, breadcrumb) == esperado


# ---------------------------------------------------------------- genérico
from fadia.adapters.generico import GenericoAdapter  # noqa: E402

_LD_BASICO = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Tapado Nobbs",
 "description":"Tapado de paño","image":["https://x.ar/a.jpg","https://x.ar/b.jpg"],
 "brand":{"@type":"Brand","name":"Markova"},"sku":"TN-001",
 "offers":{"@type":"Offer","price":"359000","priceCurrency":"ARS",
           "availability":"https://schema.org/InStock"}}
</script></head><body></body></html>"""


def _generico(slug="markova"):
    return GenericoAdapter(
        Store(slug=slug, name=slug, domain=f"{slug}.com", platform="generico"),
        fetcher=None)


def test_generico_lee_jsonld_product():
    import asyncio
    p = asyncio.run(_generico().parse_html("https://markova.com/p/tapado-nobbs", _LD_BASICO))
    assert p is not None
    assert p.title == "Tapado Nobbs"
    assert p.price_min.amount_cents == 35900000
    assert p.brand == "Markova"
    assert p.availability is Availability.IN_STOCK
    assert len(p.images) == 2
    assert p.category == "outerwear"


def test_generico_encuentra_product_dentro_de_graph():
    """Muchos WordPress anidan el Product en @graph; buscar solo en la raíz
    lo pierde y el sitio queda marcado como 'sin datos'."""
    import asyncio, json as _j
    envuelto = _LD_BASICO.replace(
        '{"@context":"https://schema.org","@type":"Product"',
        '{"@context":"https://schema.org","@graph":[{"@type":"WebSite"},{"@type":"Product"')
    envuelto = envuelto.replace('"availability":"https://schema.org/InStock"}}',
                                '"availability":"https://schema.org/InStock"}}]}')
    p = asyncio.run(_generico().parse_html("https://markova.com/p/x", envuelto))
    assert p is not None and p.price_min.amount_cents == 35900000


def test_generico_sin_precio_no_es_producto():
    """Sin precio no hay ficha utilizable: mejor descartarla que guardar
    un producto de $0 que después aparece primero al ordenar por precio."""
    import asyncio
    sin = _LD_BASICO.replace('"price":"359000",', "")
    assert asyncio.run(_generico().parse_html("https://markova.com/p/x", sin)) is None


def test_generico_ordena_probables_primero():
    urls = ["https://x.ar/contacto/", "https://x.ar/blog/nota/",
            "https://x.ar/una-remera", "https://x.ar/productos/vestido"]
    orden = GenericoAdapter._ordenar(urls)
    assert orden[0].endswith("/productos/vestido")
    assert not any("/blog/" in u or "/contacto/" in u for u in orden)


def test_generico_aggregate_offer():
    """AggregateOffer envuelve las ofertas reales; leer `price` del wrapper
    devuelve None y el producto se pierde."""
    import asyncio
    agg = _LD_BASICO.replace(
        '"offers":{"@type":"Offer","price":"359000","priceCurrency":"ARS",'
        '"availability":"https://schema.org/InStock"}',
        '"offers":{"@type":"AggregateOffer","lowPrice":"359000","highPrice":"420000",'
        '"priceCurrency":"ARS"}')
    p = asyncio.run(_generico().parse_html("https://markova.com/p/x", agg))
    assert p is not None and p.price_min.amount_cents == 35900000
