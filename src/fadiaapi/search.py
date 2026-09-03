"""Búsqueda: filtros duros en SQL + similitud semántica en el mismo plan.

El modelo NO busca. Busca Postgres. Gemma redacta la respuesta sobre las
filas que Postgres devolvió.

Es una decisión, no una limitación: un LLM que "elige" productos alucina
precios y stock, y acá los dos son verificables. Además Gemma 4 tarda ~30 s
por respuesta — meterlo en el camino de la búsqueda haría el filtrado
inutilizablemente lento.

Los filtros de precio y talle salen por reglas, no por LLM: son regulares,
rápidos y auditables. El embedding se encarga de lo que las reglas no
capturan ("algo canchero para una fiesta").
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from fadia.normalize.text import strip_accents

# 'menos de 20 lucas', 'hasta $15.000', 'entre 10000 y 30000'
_HASTA = re.compile(r"(?:menos de|hasta|bajo|máx(?:imo)?|max|no más de)\s*\$?\s*([\d.,]+)\s*(lucas|mil|k)?", re.I)
_DESDE = re.compile(r"(?:más de|desde|mín(?:imo)?|min|arriba de)\s*\$?\s*([\d.,]+)\s*(lucas|mil|k)?", re.I)
_ENTRE = re.compile(r"entre\s*\$?\s*([\d.,]+)\s*(?:y|a)\s*\$?\s*([\d.,]+)", re.I)
_TALLE = re.compile(r"\btalles?\s*([0-9]{1,2}|xxs|xs|s|m|l|xl|xxl)\b", re.I)

_CATEGORIAS = {
    "dresses": ("vestido", "vestidos"), "jeans": ("jean", "jeans", "denim"),
    "pants": ("pantalon", "pantalón", "pantalones", "cargo", "calza"),
    "skirts": ("pollera", "polleras", "falda"), "shorts": ("short", "shorts", "bermuda"),
    "tops": ("top", "tops", "remera", "remeras", "camisa", "blusa", "musculosa", "chomba"),
    "knitwear": ("sweater", "buzo", "buzos", "cardigan", "hoodie"),
    "outerwear": ("campera", "camperas", "abrigo", "tapado", "saco", "blazer"),
    "shoes": ("zapatilla", "zapatillas", "zapato", "bota", "botas", "sandalia"),
    "bags": ("cartera", "bolso", "mochila"), "jewelry": ("collar", "anillo", "aros", "pulsera"),
    "lingerie": ("lenceria", "lencería", "corpiño", "bombacha", "pijama"),
    "swimwear": ("bikini", "malla", "mallas"),
}

# Palabras que son categoría Y tela a la vez. Ver el desempate en
# parse_filters: sin esto, todo lo hecho de denim era "jeans".
_TELA_Y_CATEGORIA = {"jean", "jeans", "denim"}

_COLORES = {
    "black": ("negro", "negra", "negros"), "white": ("blanco", "blanca", "off white"),
    "blue": ("azul", "celeste", "navy"), "red": ("rojo", "roja", "bordo", "bordó"),
    "green": ("verde",), "pink": ("rosa", "rosado", "fucsia"),
    "beige": ("beige", "nude", "camel", "crudo"), "brown": ("marron", "marrón", "chocolate"),
    "grey": ("gris",), "yellow": ("amarillo", "mostaza"), "purple": ("violeta", "lila"),
}


# ---------------------------------------------------------------- facetas
# Lección de mirar a Daydream de cerca: sus sugerencias de refinamiento no
# son filtros genéricos ("más barato", "otro color") sino VOCABULARIO DEL
# RUBRO agrupado por eje — silueta (Slip, A-line, Halter), tela (Satén,
# Seda), ocasión (Black Tie), largo (Midi, Floor Length). Eso le enseña al
# usuario a pedir mejor, y de paso revela qué hay en el catálogo.
#
# Acá el vocabulario es rioplatense y sale de los propios títulos: morley,
# frisa, bengalina y gabardina aparecen miles de veces en los catálogos
# argentinos y no existen en el inglés de moda.

EJES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "tela": [
        ("morley", ("morley",)), ("frisa", ("frisa", "frisado")),
        ("bengalina", ("bengalina",)), ("microfibra", ("microfibra",)),
        ("gabardina", ("gabardina",)), ("lino", ("lino",)),
        ("algodón", ("algodon", "algodón")), ("seda", ("seda",)),
        ("encaje", ("encaje", "puntilla")), ("tul", ("tul",)),
        ("cuero", ("cuero", "ecocuero")), ("denim", ("denim", "jean")),
        ("lana", ("lana", "lanilla")), ("viscosa", ("viscosa", "modal")),
        ("lycra", ("lycra", "elastizad")), ("rústico", ("rustico", "rústico")),
        ("satén", ("saten", "satén", "satinad")), ("corderoy", ("corderoy", "pana")),
    ],
    "silueta": [
        ("oversize", ("oversize", "amplio", "holgad")),
        ("entallado", ("entallad", "al cuerpo", "ajustad", "slim")),
        ("wide leg", ("wide leg", "palazzo", "pierna ancha")),
        ("recto", ("recto", "straight")),
        ("tiro alto", ("tiro alto",)), ("tiro medio", ("tiro medio",)),
        ("cropped", ("crop", "corto")), ("largo", ("largo", "maxi")),
        ("midi", ("midi",)), ("mini", ("mini",)),
        ("babucha", ("babucha", "jogger")), ("mom fit", ("mom fit", "mom ")),
    ],
    "detalle": [
        ("con capucha", ("capucha", "hoodie")), ("sin mangas", ("sin manga", "musculosa")),
        ("manga larga", ("manga larga",)), ("escote v", ("escote v", "cuello v")),
        ("estampado", ("estampad", "print", "floral")), ("liso", ("liso",)),
        ("tajo", ("tajo",)), ("volados", ("volado", "volante")),
        ("bordado", ("bordad",)), ("con brillo", ("brillo", "lentejuela", "glitter")),
    ],
}


def _texto_de(r: dict) -> str:
    return strip_accents(
        f"{r.get('title') or ''} {r.get('description') or ''}").lower()


def facetas(rows: list[dict], ya_usados: set[str] | None = None) -> list[dict]:
    """Qué ejes de refinamiento tienen sentido para ESTE resultado.

    Solo se ofrece lo que realmente aparece en las filas devueltas: una
    sugerencia que no filtra nada es peor que ninguna. Y se saltean los
    ejes ya usados en el hilo, para que la conversación avance en vez de
    dar vueltas sobre lo mismo.
    """
    ya = ya_usados or set()
    textos = [_texto_de(r) for r in rows]
    if not textos:
        return []

    salida: list[dict] = []
    for eje, terminos in EJES.items():
        if eje in ya:
            continue
        presentes = []
        for etiqueta, claves in terminos:
            n = sum(1 for t in textos if any(k in t for k in claves))
            # ni lo que no está, ni lo que está en todo: eso no discrimina
            if 0 < n < len(textos):
                presentes.append((etiqueta, n))
        presentes.sort(key=lambda p: -p[1])
        if len(presentes) >= 2:
            salida.append({"eje": eje,
                           "opciones": [e for e, _ in presentes[:4]]})
    return salida[:2]


def _to_cents(raw: str, unit: str | None) -> int:
    n = float(raw.replace(".", "").replace(",", "."))
    if unit and unit.lower() in ("lucas", "mil", "k"):
        n *= 1000
    return int(n * 100)


# El catálogo tiene dos canales con lógicas de compra incompatibles:
# en Avellaneda comprás una CURVA para revender; en una marca comprás una
# unidad para vos. Mezclarlos en una sola lista confunde las dos búsquedas.
CANAL_MAYORISTA = "avellaneda"


@dataclass
class Filters:
    canal: str | None = None          # 'mayorista' | 'marcas' | None (todo)
    genero: str | None = None         # 'women' | 'men' | 'sin' | None (todo)
    tela: str | None = None           # morley, bengalina, encaje, ...
    detalle: str | None = None        # tajo, capucha, estampado, ...
    max_cents: int | None = None
    min_cents: int | None = None
    category: str | None = None
    color: str | None = None
    size: str | None = None
    store: str | None = None
    in_stock: bool = True
    applied: list[str] = field(default_factory=list)


def parse_filters(q: str, hilo: list[str] | None = None) -> Filters:
    """Extrae de la pregunta lo que se puede filtrar de forma exacta.

    `hilo` son las preguntas anteriores, de la más vieja a la más nueva.
    Un seguimiento como "¿y en negro?" no dice qué prenda es, y "algo más
    barato" tampoco: hay que ACUMULAR los filtros de todo el hilo, no solo
    de la última pregunta. Mirar únicamente la anterior perdía la categoría
    en el tercer turno y la búsqueda se iba a ropa interior.

    Lo nuevo pisa a lo viejo: si antes pidió negro y ahora dice rojo, vale
    rojo.
    """
    f = Filters()
    low = q.lower()
    for anterior in (hilo or []):
        base = parse_filters(anterior)          # sin hilo: corta la recursión
        for campo in ("category", "color", "size", "max_cents", "min_cents",
                      "tela", "detalle"):
            if (v := getattr(base, campo)) is not None:
                setattr(f, campo, v)
    heredados = [c for c in ("category", "color", "size", "tela", "detalle")
                 if getattr(f, c)]
    if heredados:
        f.applied.append("del hilo: " + ", ".join(
            str(getattr(f, c)) for c in heredados))

    if m := _ENTRE.search(low):
        f.min_cents, f.max_cents = _to_cents(m.group(1), None), _to_cents(m.group(2), None)
        f.applied.append(f"precio entre ${f.min_cents/100:,.0f} y ${f.max_cents/100:,.0f}")
    else:
        if m := _HASTA.search(low):
            f.max_cents = _to_cents(m.group(1), m.group(2))
            f.applied.append(f"precio hasta ${f.max_cents/100:,.0f}")
        if m := _DESDE.search(low):
            f.min_cents = _to_cents(m.group(1), m.group(2))
            f.applied.append(f"precio desde ${f.min_cents/100:,.0f}")

    if m := _TALLE.search(low):
        f.size = m.group(1).upper() if not m.group(1).isdigit() else m.group(1)
        f.applied.append(f"talle {f.size}")

    # plural opcional: "sandalia" tiene que capturar "sandalias".
    # El diccionario de normalize/taxonomy ya lo hacía; este quedó afuera
    # y "sandalias para un casamiento" devolvía abrigos.
    #
    # Y no alcanza con cortar en la primera coincidencia: "jean" es a la vez
    # categoría y TELA, y como `jeans` se evalúa antes que `skirts`, una
    # "pollera de jean" se filtraba como pantalón. Lo mismo con "campera de
    # jean" y "camisa de jean". La prenda concreta le gana a la categoría
    # deducida solo de la tela.
    candidatos: list[tuple[str, bool]] = []
    for cat, palabras in _CATEGORIAS.items():
        hits = [w for w in palabras if re.search(rf"\b{w}s?\b", low)]
        if hits:
            candidatos.append((cat, all(w in _TELA_Y_CATEGORIA for w in hits)))
    if candidatos:
        prendas = [c for c, solo_tela in candidatos if not solo_tela]
        f.category = prendas[0] if prendas else candidatos[0][0]
        f.applied.append(f"categoría {f.category}")

    for col, palabras in _COLORES.items():
        if any(re.search(rf"\b{w}s?\b", low) for w in palabras):
            f.color = col
            f.applied.append(f"color {col}")
            break

    # Tela y detalle: son la mitad del vocabulario con el que se pide ropa
    # en Argentina ("de morley", "con tajo") y sin esto el buscador los
    # trataba como nombre de marca y no encontraba nada.
    for etiqueta, claves in EJES["tela"]:
        if any(re.search(rf"\b{re.escape(k)}", low) for k in claves):
            f.tela = etiqueta; f.applied.append(f"tela {etiqueta}"); break
    for etiqueta, claves in EJES["detalle"]:
        if any(re.search(rf"\b{re.escape(k)}", low) for k in claves):
            f.detalle = etiqueta; f.applied.append(etiqueta); break

    if re.search(r"\b(hombre|masculino|caballero|men)s?\b", low):
        f.genero = "men"; f.applied.append("hombre")
    elif re.search(r"\b(mujer|femenino|dama|women)s?\b", low):
        f.genero = "women"; f.applied.append("mujer")
    elif re.search(r"\b(ni[nñ]o|ni[nñ]a|beb[eé]|kids|infantil)s?\b", low):
        f.genero = "kids"; f.applied.append("niños")

    if "mayorista" in low or "avellaneda" in low or "por mayor" in low:
        f.canal = "mayorista"
        f.applied.append("solo mayoristas")
    return f


# Cuántos turnos previos entran en el vector. Más que esto y el hilo empieza
# a arrastrar intenciones ya abandonadas.
MAX_TURNOS_VECTOR = 3
# Tope de repeticiones del mensaje actual: sin esto, un "sí" después de una
# pregunta larga se repetiría decenas de veces y el vector sería puro ruido.
MAX_REPETICIONES = 3


def texto_de_busqueda(message: str, hilo: list[str] | None = None) -> str:
    """El texto que se convierte en vector.

    Los filtros duros se acumulan solos (ver `parse_filters`), pero todo lo
    que NO es filtrable vive únicamente en el embedding: la ocasión
    ("para un casamiento"), el registro, el uso. Como el vector se armaba
    solo con el último mensaje, en el tercer turno "algo más largo" daba
    abrigos: la ocasión se había evaporado dos turnos antes.

    El mensaje actual se repite para que pese más que cualquier turno
    anterior — con mean pooling, la proporción de tokens es la proporción de
    peso en el vector. Se compara contra el turno MÁS LARGO, no contra la
    suma: exigir que gane a todo el hilo junto obligaría a tirar los turnos
    viejos, que es justamente lo que se quiere conservar.
    """
    actual = (message or "").strip()
    previos = [p.strip() for p in (hilo or [])[-MAX_TURNOS_VECTOR:] if p and p.strip()]
    if not actual:
        return " | ".join(previos)
    if not previos:
        return actual

    mas_largo = max(len(p) for p in previos)
    veces = 1
    while len(actual) * veces < mas_largo and veces < MAX_REPETICIONES:
        veces += 1
    veces = max(veces, 2)       # nunca menos del doble de su peso natural
    return " | ".join([*previos, *([actual] * veces)])


class SearchService:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    # Orden en que se aflojan los filtros cuando no hay resultados.
    # El precio es lo último: si alguien dice "hasta 30 lucas", mostrarle algo
    # de 90 no es una respuesta, es ruido.
    # `canal` y `genero` no están en la lista a propósito: los eligió el
    # usuario con los selectores, y aflojarlos le devolvería justo el
    # catálogo que descartó.
    RELAX_ORDER = ("detalle", "tela", "color", "size", "category", "store", "min_cents")

    def search_relaxed(self, *, embedding: list[float] | None, filters: Filters,
                       text: str = "", limit: int = 12,
                       offset: int = 0) -> tuple[list[dict], list[str]]:
        """Busca con todo; si no hay nada, va soltando filtros de a uno.

        Una consulta razonable como "vestido negro talle 40 menos de 30 lucas"
        devuelve cero en un catálogo mayorista donde los talles no se publican
        por producto. Devolver una pantalla vacía es peor que devolver algo
        pertinente diciendo qué se aflojó.
        """
        rows = self.search(embedding=embedding, filters=filters, text=text,
                           limit=limit, offset=offset)
        if rows:
            return rows, []

        aflojados: list[str] = []
        for campo in self.RELAX_ORDER:
            if getattr(filters, campo, None) in (None, ""):
                continue
            valor = getattr(filters, campo)
            setattr(filters, campo, None)
            aflojados.append(f"{campo}={valor}")
            rows = self.search(embedding=embedding, filters=filters, text=text, limit=limit)
            if rows:
                return rows, aflojados
        return rows, aflojados

    def search(self, *, embedding: list[float] | None, filters: Filters,
               text: str = "", limit: int = 12, offset: int = 0) -> list[dict]:
        where: list[sql.Composable] = []
        params: list = []

        if filters.in_stock:
            where.append(sql.SQL("p.availability = 'in_stock'"))
        # Solo lo visto en la última corrida de cada tienda.
        #
        # La ingesta es un upsert: nunca retira lo que desapareció del
        # origen. Cuando Zara se recosechó con deduplicación, las 4.000
        # fichas duplicadas de la corrida anterior siguieron ahí y el mismo
        # vestido aparecía tres veces en la grilla. Comparar contra
        # `store.last_crawl` las deja fuera sin borrar nada: el histórico de
        # precios se conserva intacto.
        where.append(sql.SQL(
            "(s.last_crawl IS NULL OR p.scraped_at >= s.last_crawl - interval '12 hours')"))
        if filters.max_cents:
            where.append(sql.SQL("p.price_min_cents <= %s")); params.append(filters.max_cents)
        if filters.min_cents:
            where.append(sql.SQL("p.price_min_cents >= %s")); params.append(filters.min_cents)
        if filters.category:
            where.append(sql.SQL("p.category = %s")); params.append(filters.category)
        if filters.store:
            where.append(sql.SQL("p.store_slug = %s")); params.append(filters.store)
        if filters.genero == "sin":
            # "Sin género" no es un cajón de descarte: es el 24 % del catálogo
            # cuya línea la tienda no declara. Merece ser navegable, no
            # esconderse.
            where.append(sql.SQL("p.gender NOT IN ('women','men','kids')"))
        elif filters.genero in ("women", "men", "kids"):
            where.append(sql.SQL("p.gender = %s")); params.append(filters.genero)
        for campo, valor in (("tela", filters.tela), ("detalle", filters.detalle)):
            if not valor:
                continue
            claves = dict(EJES[campo]).get(valor, (valor,))
            # También la descripción generada por visión: para 43.383
            # productos es el único lugar donde dice de qué está hecha la
            # prenda. Sin esto, describirlos no servía de nada — el filtro
            # duro los dejaba afuera antes de llegar al ranking.
            cond = sql.SQL(" OR ").join(
                sql.SQL("(p.title ILIKE %s OR coalesce(p.description,'') ILIKE %s "
                        "OR coalesce(p.description_ia,'') ILIKE %s)")
                for _ in claves)
            where.append(sql.SQL("({})").format(cond))
            for k in claves:
                params.extend([f"%{k}%", f"%{k}%", f"%{k}%"])
        if filters.canal == "mayorista":
            where.append(sql.SQL("p.store_slug = %s")); params.append(CANAL_MAYORISTA)
        elif filters.canal == "marcas":
            where.append(sql.SQL("p.store_slug <> %s")); params.append(CANAL_MAYORISTA)
        if filters.size:
            where.append(sql.SQL("%s = ANY(p.sizes_available)")); params.append(filters.size)
        if filters.color:
            where.append(sql.SQL("""EXISTS (SELECT 1 FROM variant v
                WHERE v.product_uid = p.product_uid
                  AND v.color_normalized = %s AND v.availability = 'in_stock')"""))
            params.append(filters.color)

        # orden: si hay embedding, por distancia coseno; si no, por trigrama
        if embedding is not None:
            order = sql.SQL("p.embedding <=> %s::vector")
            score = sql.SQL("(p.embedding <=> %s::vector) AS distancia")
            vec = json.dumps(embedding)
            score_params = [vec]
            order_params = [vec]
            where.append(sql.SQL("p.embedding IS NOT NULL"))
        else:
            order = sql.SQL("similarity(p.title_normalized, %s) DESC")
            score = sql.SQL("similarity(p.title_normalized, %s) AS distancia")
            score_params = [text]
            order_params = [text]

        # DISTINCT ON por título+marca: Zara publica cada colorway como un
        # producto aparte con el MISMO nombre, así que ocho resultados podían
        # ser cuatro prendas repetidas. No son datos duplicados —son SKUs
        # distintos— así que se agrupan al mostrar, no al ingerir.
        query = sql.SQL("""
            SELECT DISTINCT ON (p.title_normalized, coalesce(p.brand,''))
                   p.product_uid, p.title, p.url, p.brand, p.category, p.store_slug,
                   p.price_min_cents, p.price_retail_cents,
                   p.min_purchase, p.min_qty, p.min_unit,
                   p.seller_name, p.seller_address, p.sizes_available,
                   p.colors_available, p.images, p.description, {score}
            FROM product p
            JOIN store s ON s.slug = p.store_slug
            WHERE {where}
            ORDER BY p.title_normalized, coalesce(p.brand,''), {order}
            LIMIT %s
        """).format(
            score=score,
            where=sql.SQL(" AND ").join(where) if where else sql.SQL("TRUE"),
            order=order,
        )
        # DISTINCT ON obliga a que el ORDER BY empiece por sus claves, lo que
        # rompe el orden por relevancia. Se envuelve para recuperarlo.
        query = sql.SQL(
            "SELECT * FROM ({q}) t ORDER BY t.distancia {dir} LIMIT %s OFFSET %s").format(
            q=query, dir=sql.SQL("ASC" if embedding is not None else "DESC"))

        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            # HNSW es un índice APROXIMADO y post-filtra: primero busca los
            # ~40 vecinos más cercanos y recién después aplica el WHERE. Con
            # un filtro selectivo (categoría + tienda) casi todos los
            # candidatos se caen y la consulta devuelve 0 filas aunque en la
            # tabla haya 1.938 que cumplen.
            #
            # `iterative_scan` hace que pgvector siga ampliando la búsqueda
            # hasta juntar LIMIT filas que pasen el filtro. Sin esto, el
            # buscador miente en silencio: devuelve poco y parece que no hay
            # stock. (pgvector >= 0.8)
            if embedding is not None:
                conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                conn.execute("SET LOCAL hnsw.ef_search = 200")
            rows = conn.execute(
                query, score_params + params + order_params
                + [(limit + offset) * 4, limit, offset]).fetchall()

        for r in rows:
            imgs = r.get("images") or []
            if isinstance(imgs, str):
                imgs = json.loads(imgs)
            urls = [i["url"] for i in imgs if i.get("url")][:3]
            r["image"] = urls[0] if urls else None
            # la segunda imagen alimenta el swap al pasar el mouse: con
            # mediana de 3 fotos por ficha, sale gratis
            r["images"] = urls
            r["price"] = r.pop("price_min_cents") / 100
            r["price_retail"] = (r.pop("price_retail_cents") or 0) / 100 or None
            if r.get("distancia") is not None:
                r["distancia"] = round(float(r["distancia"]), 4)
        return rows

    def stats(self) -> dict:
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute("""
                SELECT (SELECT count(*) FROM product) AS productos,
                       (SELECT count(*) FROM variant) AS variantes,
                       (SELECT count(*) FROM price_point) AS price_points,
                       (SELECT count(*) FROM product WHERE embedding IS NOT NULL) AS con_embedding,
                       (SELECT count(DISTINCT seller_name) FROM product
                         WHERE seller_name IS NOT NULL) AS locales,
                       (SELECT count(DISTINCT store_slug) FROM product
                         WHERE store_slug <> 'avellaneda') AS marcas,
                       (SELECT count(*) FROM store) AS tiendas
            """).fetchone()
            por_tienda = conn.execute("""
                SELECT store_slug, count(*) AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY price_min_cents)/100 AS precio_mediano
                FROM product GROUP BY store_slug ORDER BY n DESC
            """).fetchall()
            cats = conn.execute("""
                SELECT coalesce(category,'(sin clasificar)') AS category, count(*) AS n
                FROM product GROUP BY 1 ORDER BY n DESC LIMIT 10
            """).fetchall()
        return {**row, "por_tienda": por_tienda, "categorias": cats}
