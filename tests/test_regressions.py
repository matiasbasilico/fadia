"""Regresiones de los incidentes reales de este proyecto.

Cada test corresponde a un bug que llegó a producir datos malos. Corren
sobre HTML congelado, así que fallan en CI en el momento en que alguien
rompe el parser — no seis semanas después, mirando el dataset.
"""
import pytest

from conftest import fixture

from fadia.models import Product, SizeSystem
from fadia.normalize.money import parse_ars
from fadia.normalize.size import normalize_size, suspicious_color_set
from fadia.validate import Severity, validate


# ---------------------------------------------------------------- incidente 1
# El JSON-LD de Tiendanube publica el precio DE LISTA cuando hay promo.
# DRESS JAMIE decía $36.500 y se vendía a $23.725.
def test_precio_sale_de_data_variants_no_del_jsonld(tiendanube_parse):
    p = tiendanube_parse(fixture("tn_sunny_dress_jamie.html"),
                         "https://www.sunnyclothing.ar/productos/dress-jamie/")
    assert p is not None
    assert p.price_min.amount_cents == 2372500, "debe ser el precio de venta"
    assert p.price_min.compare_at_cents == 3650000, "el de lista va a compare_at"
    assert p.price_min.discount_pct == pytest.approx(35.0, abs=0.5)


# ---------------------------------------------------------------- incidente 2
# option0/1/2 son POSICIONALES. En un vestido es color, en un jean es talle.
def test_option0_es_color_en_vestido(tiendanube_parse):
    p = tiendanube_parse(fixture("tn_sunny_dress_jamie.html"),
                         "https://www.sunnyclothing.ar/productos/dress-jamie/")
    colores = {v.color.raw for v in p.variants if v.color}
    assert colores == {"Negro", "Azul", "Marrón", "Off white"}
    assert all(v.size is None for v in p.variants)


def test_option0_es_talle_en_jean(tiendanube_parse):
    p = tiendanube_parse(fixture("tn_sunny_jean_romee.html"),
                         "https://www.sunnyclothing.ar/productos/jean-romee/")
    talles = {v.size.normalized for v in p.variants if v.size}
    assert talles == {"36", "38", "40", "42", "44"}
    assert all(v.color is None for v in p.variants)


# ---------------------------------------------------------------- incidente 3
# Un tema sin <label> + talles de nena (4,6,8,10,12) guardaba los talles
# en el campo color. La regla solo aceptaba 32-60.
def test_talles_de_nena_no_van_a_color(tiendanube_parse):
    p = tiendanube_parse(fixture("tn_alabama_kids_sizes.html"),
                         "https://alabamatienda.com.ar/productos/pantalon-babucha-peter-pan-negro/",
                         slug="alabamatienda")
    assert p is not None
    assert all(v.color is None for v in p.variants), "los talles no son colores"
    talles = {v.size.normalized for v in p.variants if v.size}
    assert talles, "debe haber talles"
    assert talles <= {str(n) for n in range(2, 19)}, "talles de niño por edad"
    assert all(v.size.system is SizeSystem.KIDS_NUMERIC for v in p.variants if v.size)


@pytest.mark.parametrize("raw,esperado,sistema", [
    ("38", "38", SizeSystem.AR_NUMERIC),
    ("Talle 40", "40", SizeSystem.AR_NUMERIC),
    ("6", "6", SizeSystem.KIDS_NUMERIC),
    ("l", "L", SizeSystem.ALPHA),
    ("XG", "XL", SizeSystem.ALPHA),
    ("Talle único", "ONE SIZE", SizeSystem.ONE_SIZE),
    ("40x40", "40x40", SizeSystem.DIMENSION),
    ("70X50", "70x50", SizeSystem.DIMENSION),
    ("25", "25", SizeSystem.SHOE_AR),
])
def test_normalizacion_de_talles(raw, esperado, sistema):
    s = normalize_size(raw)
    assert s.normalized == esperado
    assert s.system is sistema


# ---------------------------------------------------------------- precios AR
@pytest.mark.parametrize("raw,cents", [
    ("$36.500,00", 3650000),
    ("36.500", 3650000),      # punto como separador de miles
    ("23725", 2372500),
    ("$ 12.000", 1200000),
    ("1.234.567,89", 123456789),
    (10000, 1000000),
    (None, None),
    ("", None),
])
def test_precios_argentinos(raw, cents):
    assert parse_ars(raw) == cents


# ---------------------------------------------------------------- marketplace
def test_avellaneda_extrae_mayorista_minorista_y_local(avellaneda_parse):
    p = avellaneda_parse(fixture("ava_palazzo.html"),
                         "https://www.avellanedaauntoque.com/p/01NZ0XA-palazzo-de-bengalina")
    assert p is not None
    assert p.price_min.amount_cents == 1000000, "por mayor $10.000"
    assert p.price_retail is not None and p.price_retail.amount_cents == 1200000
    assert p.seller is not None
    assert p.seller.name == "Mattis"
    assert p.seller.address == "Concordia 360"
    assert p.min_purchase and "CURVA" in p.min_purchase.upper()
    assert len(p.images) == 3


# ---------------------------------------------------------------- arnés
def _producto(uid: str, **kw) -> Product:
    base = dict(
        product_uid=uid, store_slug="t", external_id=uid.split(":")[-1],
        url=f"https://t.ar/p/{uid.split(':')[-1]}", handle="h",
        title="Remera Lisa", title_normalized="remera lisa",
        price_min={"amount_cents": 1000000}, price_max={"amount_cents": 1000000},
    )
    base.update(kw)
    return Product(**base)


def test_arnes_detecta_colores_numericos():
    malos = [
        _producto(f"t:{i}", variants=[
            {"external_id": f"{i}a", "color": {"raw": "4"}, "price": {"amount_cents": 1000000}},
            {"external_id": f"{i}b", "color": {"raw": "6"}, "price": {"amount_cents": 1000000}},
        ]) for i in range(20)
    ]
    hallazgos = validate(malos)
    hit = [f for f in hallazgos if f.check == "semantica:color-numerico"]
    assert hit and hit[0].severity is Severity.BLOCK


def test_arnes_detecta_html_sin_decodificar():
    """El incidente del brotli: HTTP 200 y bytes comprimidos como texto."""
    basura = [_producto(f"t:{i}", title="\x1b\x08Rem��ra",
                        title_normalized="rem") for i in range(10)]
    hit = [f for f in validate(basura) if f.check == "contenido:no-decodificado"]
    assert hit and hit[0].severity is Severity.BLOCK


def test_arnes_detecta_salto_de_unidad_en_precio():
    from fadia.validate import Baseline
    ps = [_producto(f"t:{i}", price_min={"amount_cents": 100000000},
                    price_max={"amount_cents": 100000000}) for i in range(30)]
    base = Baseline(median_price_cents=1000000)          # x100
    hit = [f for f in validate(ps, baseline=base) if f.check == "deriva:precio"]
    assert hit and hit[0].severity is Severity.BLOCK


def test_arnes_detecta_crawl_incompleto():
    ps = [_producto(f"t:{i}") for i in range(50)]
    hit = [f for f in validate(ps, expected_urls=1000) if f.check == "completitud"]
    assert hit and hit[0].severity is Severity.BLOCK


def test_arnes_no_se_queja_de_un_lote_sano(tiendanube_parse):
    ps = [
        tiendanube_parse(fixture("tn_sunny_dress_jamie.html"),
                         "https://www.sunnyclothing.ar/productos/dress-jamie/"),
        tiendanube_parse(fixture("tn_sunny_jean_romee.html"),
                         "https://www.sunnyclothing.ar/productos/jean-romee/"),
    ]
    bloqueantes = [f for f in validate(ps) if f.severity is Severity.BLOCK]
    assert bloqueantes == []


def test_suspicious_color_set():
    assert suspicious_color_set(["4", "6", "8"])
    assert suspicious_color_set(["40x40", "50x50"])
    assert not suspicious_color_set(["Negro", "Azul"])
    assert not suspicious_color_set(["Negro"])          # uno solo no alcanza


# ── El vector se armaba solo con el último mensaje ────────────────────────
# Los filtros duros (categoría, color, talle) se acumulaban bien, pero la
# OCASIÓN no es filtrable y vivía únicamente en el embedding. Al tercer
# turno, "algo más largo" daba abrigos: el casamiento se había perdido.

def test_el_vector_conserva_el_hilo():
    from fadiaapi.search import texto_de_busqueda
    t = texto_de_busqueda("algo mas largo",
                          ["un vestido para un casamiento", "en verde"])
    assert "casamiento" in t          # la ocasión sobrevive al tercer turno
    assert "verde" in t


def test_el_mensaje_actual_pesa_mas_que_cualquier_turno_previo():
    from fadiaapi.search import texto_de_busqueda
    previos = ["un vestido para un casamiento", "en verde"]
    actual = "algo mas largo"
    t = texto_de_busqueda(actual, previos)
    peso_actual = t.count(actual) * len(actual)
    assert peso_actual > max(len(p) for p in previos)


def test_el_hilo_no_crece_sin_limite():
    from fadiaapi.search import MAX_TURNOS_VECTOR, texto_de_busqueda
    viejos = [f"turno viejo numero {i}" for i in range(8)]
    t = texto_de_busqueda("lo ultimo", viejos)
    assert "turno viejo numero 0" not in t          # los primeros se caen
    assert viejos[-MAX_TURNOS_VECTOR] in t          # los recientes quedan


def test_sin_hilo_el_texto_es_el_mensaje_tal_cual():
    from fadiaapi.search import texto_de_busqueda
    assert texto_de_busqueda("vestido negro", []) == "vestido negro"
    assert texto_de_busqueda("vestido negro", None) == "vestido negro"


def test_un_mensaje_corto_no_se_repite_sin_freno():
    from fadiaapi.search import MAX_REPETICIONES, texto_de_busqueda
    t = texto_de_busqueda("si", ["una pregunta muchisimo mas larga que la respuesta"])
    # Por tramos, no por subcadena: "muchisimo" también contiene "si".
    tramos = [x.strip() for x in t.split("|")]
    assert tramos.count("si") <= MAX_REPETICIONES


# ── "jean" es categoría Y tela a la vez ──────────────────────────────────
# `jeans` se evaluaba antes que `skirts` y cortaba en la primera
# coincidencia, así que "pollera de jean" se filtraba como pantalón. Lo
# mismo con campera y camisa. La prenda concreta tiene que ganarle a la
# categoría deducida de la tela.

@pytest.mark.parametrize("consulta,categoria", [
    ("pollera de jean con bolsillos", "skirts"),
    ("campera de jean", "outerwear"),
    ("camisa de jean", "tops"),
    ("vestido de jean", "dresses"),
    ("un jean wide leg", "jeans"),      # acá el jean SÍ es la prenda
    ("jeans azules", "jeans"),
])
def test_la_prenda_le_gana_a_la_tela(consulta, categoria):
    from fadiaapi.search import parse_filters
    f = parse_filters(consulta)
    assert f.category == categoria
    assert f.tela == "denim"            # la tela se detecta igual


def test_la_descripcion_generada_entra_al_embedding():
    """Si la tienda no publica descripción, se usa la generada por visión."""
    import importlib.util, pathlib
    ruta = pathlib.Path(__file__).resolve().parents[1] / "scripts_embed.py"
    spec = importlib.util.spec_from_file_location("_embed", ruta)
    # el script corre main() al importarse: se lee la función suelta
    fuente = ruta.read_text(encoding="utf-8")
    ns: dict = {}
    inicio = fuente.index("def texto(")
    exec(fuente[inicio:fuente.index("\nasync def main")], ns)   # noqa: S102
    texto = ns["texto"]

    generado = texto({"title": "FALDA X", "description": None,
                      "description_ia": "Pollera de jean negra"})
    assert "pollera de jean" in generado.lower()
    # la de la tienda manda cuando existe
    t = texto({"title": "FALDA X", "description": "Pollera de gabardina beige",
               "description_ia": "Pollera de jean negra"}).lower()
    assert "gabardina" in t and "jean" not in t


# ── El color era SOLO de las variantes ───────────────────────────────────
# El 49 % de las marcas no publica ningún color, así que pedir "campera
# bordó" excluía media catálogo antes de mirar nada. Y los sinónimos eran
# 11 colores supuestos: "borgoña" aparece 140 veces en las descripciones
# generadas y no estaba, así que la campera borgoña quedaba afuera.

@pytest.mark.parametrize("consulta,color", [
    ("campera bordó", "red"),
    ("campera borgoña", "red"),      # el que faltaba
    ("pantalón oliva", "green"),
    ("saco camel", "beige"),
    ("remera terracota", "brown"),
])
def test_sinonimos_de_color_medidos(consulta, color):
    from fadiaapi.search import parse_filters
    assert parse_filters(consulta).color == color
