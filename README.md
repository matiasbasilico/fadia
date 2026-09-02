# fadia

Scraper de catálogos de indumentaria argentina con una capa de normalización
canónica. Un adapter por **plataforma de ecommerce**, no por tienda.

## Correr

```bash
uv sync
PYTHONPATH=src uv run python -m fadia.cli sunnyclothing.ar --out data/sunny.jsonl
```

## Arquitectura

```
detect.py           huella de la plataforma en el HTML (tiendanube/shopify/vtex/...)
fetch.py            HTTP con rate limit por dominio + cache en disco
adapters/           un archivo por plataforma; todos devuelven Product
normalize/          money, size, color, taxonomy, text
models.py           esquema canónico (pydantic)
storage/schema.sql  Postgres + pgvector
```

## Por qué por plataforma y no por tienda

Tiendanube concentra la mayor parte de la indumentaria independiente
argentina. Un solo adapter cubre cientos de sitios: el tema cambia, el
`data-variants` y el sitemap no. Escribir un parser por tienda es trabajo
lineal que se rompe en cada rediseño.

## Notas de campo (Tiendanube)

- **`data-variants` es la fuente de verdad**, no el JSON-LD. El
  `offers.price` de schema.org publica el precio *de lista* cuando hay
  promo: en DRESS JAMIE decía $36.500 cuando se vendía a $23.725.
- **`option0/1/2` son posicionales**. En un vestido `option0` es el color;
  en un jean es el talle. Hay que leer el `<label for="variation_N">` del
  form de compra o el dataset queda con talles en el campo color.
- **El sitemap trae el catálogo completo sin paginar.** Preferilo a
  recorrer `/productos/?page=N`.
- **La galería hay que acotarla** a `.js-product-detail-img-col`: barrer el
  HTML entero mete las fotos de los carruseles de recomendados.
- `robots.txt` de Tiendanube permite `/productos/`; bloquea `/checkout/`,
  `/comprar/` y `/search/`. El crawler respeta eso y va a 1 req/s.

## Tiendas cubiertas

| Adapter | Qué cubre | Descubrimiento | Fuente de verdad |
|---|---|---|---|
| `tiendanube` | cientos de tiendas AR | `sitemap.xml` | `data-variants` (HTML) |
| `avellaneda` | marketplace, 139.044 fichas / 3.856 locales | índice de 14 sitemaps | JSON-LD + texto server-rendered |
| `shopify` | marcas de shopping | `/products.json` paginado | API |
| `vtex` | cadenas grandes | `/api/catalog_system/pub/products/search` | API |
| `magento` | marcas premium | `/graphql` | API |

Los tres de API nunca tocan HTML y **nombran sus opciones** (`options[].name`,
`variations`, `configurable_options[].label`), así que no hay que inferir si
`option0` es talle o color como en Tiendanube.

## Notas de campo (adapters de API)

**Shopify** — `compare_at_price` llega como `"0.00"`, no `null`, cuando no hay
descuento: tomarlo literal genera descuentos del 100 % en todo el catálogo.
`products.json` tampoco publica stock por variante (`inventory_quantity` es
`null`); el único dato es el booleano `available`, así que `stock` queda en
`None` en vez de un 0 inventado.

**VTEX** — responde `206 Partial Content` y **el total va en el header**
`resources: 0-49/779`, no en el cuerpo. La ventana máxima es de 50 productos y
el offset tope es 2.500: para catálogos grandes hay que particionar por
categoría. A cambio es el más completo — `commertialOffer` trae cantidad
disponible real.

**Magento** — el esquema cambia entre versiones: `uid` no existe antes de
2.4.2 y su sola presencia hace fallar la query entera (bowen.com.ar devolvía
cero productos por eso). Hay una query mínima de respaldo. GraphQL además mete
`null` **adentro** de las listas, así que hay que filtrar por tipo o el parseo
revienta a mitad del catálogo.

**Dominio ≠ tienda.** `babycottons.com.ar` y `carocuore.com.ar` responden ambos
`store_name: "Caro Cuore AR Store View"` con `base_url` carocuore.com: son la
misma instancia Magento detrás de dos dominios. `store_identity()` lo detecta;
sin eso el mismo catálogo se ingiere dos veces con `product_uid` distintos.

## Notas de campo (Avellaneda a un Toque)

- **Los listados por rubro se randomizan.** Tres llamadas seguidas a
  `/r/mujer/jeans` devuelven 60 productos sin solapamiento. Sirven para
  descubrir, no para enumerar: el único camino al 100% es el sitemap.
- **`robots.txt` prohíbe `/api/`.** Los datos se toman del JSON-LD y del
  HTML server-rendered, que son públicos. El sitio además declara
  `Content-Signal: ai-train=no`.
- **`lastmod` es inútil para crawl incremental**: las 139.044 URLs traen la
  misma fecha, la de generación del sitemap. Hay que diffear por
  `content_hash`.
- **Doble precio.** `lowPrice` es por mayor, `highPrice` por menor, y la
  compra mínima ("curva") define cuánto hay que llevar. Es texto libre por
  local: se parsea lo estructurable y se conserva el crudo.

## Stack completo (Docker)

```bash
# 1. LM Studio en el HOST con el modelo cargado
#    (Docker no puede usar la GPU de la Mac; por eso no va en un contenedor)
lms server start
lms load google/gemma-4-e4b
lms load text-embedding-nomic-embed-text-v1.5

# 2. levantar base + api + web
docker compose up -d --build

# 3. ingerir y embeber
uv run python scripts_ingest.py data/sunny_full.jsonl        --store sunnyclothing
uv run python scripts_ingest.py data/avellaneda/products.jsonl --store avellaneda
uv run python scripts_embed.py
```

| Servicio | Puerto | Qué es |
|---|---|---|
| `web` | http://localhost:5173 | Chat + grilla de resultados |
| `api` | http://localhost:8080 | FastAPI: `/search`, `/chat` (SSE), `/stats`, `/product/{uid}` |
| `db` | localhost:5433 | Postgres 17 + pgvector + pg_trgm |

## Cómo busca

El modelo **no** busca: busca Postgres. Gemma redacta sobre las filas devueltas
y solo puede citar del contexto numerado que recibe. Los filtros de precio,
talle, categoría y color salen por reglas (rápidos y auditables); el embedding
cubre lo que las reglas no capturan.

Si el filtro estricto no devuelve nada, se aflojan de a uno en orden
`color → talle → categoría → tienda`, y se le avisa al usuario qué se aflojó.
El precio nunca se afloja.

### pgvector: `iterative_scan` no es opcional

HNSW es un índice **aproximado y post-filtrante**: busca los ~40 vecinos más
cercanos y recién después aplica el `WHERE`. Con un filtro selectivo
(`category='tops' AND store_slug='avellaneda'`) la consulta devolvía **0 filas
teniendo 1.938 que cumplían**.

```sql
SET LOCAL hnsw.iterative_scan = relaxed_order;   -- pgvector >= 0.8
SET LOCAL hnsw.ef_search = 200;
```

Sin esto el buscador miente en silencio: devuelve poco y parece falta de stock.

## Validación

```bash
uv run pytest -q                                   # 28 tests sobre HTML congelado
uv run python scripts_validate.py data/sunny_full.jsonl
```

`scripts_ingest.py` corre el arnés **antes** de escribir: si hay hallazgos
BLOQUEANTES no ingiere (`--force` para saltearlo).


## Latencia del chat

Medido de punta a punta, mediana sobre 5 preguntas:

| | antes | después |
|---|---|---|
| grilla en pantalla | 600 ms | **60 ms** |
| primer texto | 22,9 s | **0,7 s** |
| respuesta completa | ~30 s | **1,9 s** |

Dos cambios, ninguno de los cuales es "usar un modelo más chico".

### 1. Apagar la cadena de razonamiento

Gemma 4 razona por defecto y gastaba ~2.200 caracteres de `reasoning_content`
antes de escribir la primera palabra. En esta tarea no aporta: el trabajo
difícil —elegir qué productos son relevantes— ya lo hizo Postgres.

```
con razonamiento    29,4 s · 22,9 s hasta el primer texto
sin razonamiento     6,2 s ·  0,5 s hasta el primer texto
```

Verificado que no degrada lo que importa: sigue negándose a inventar productos
ausentes del contexto y atributos que el contexto no trae.
`REASONING_EFFORT=low|medium|high` lo vuelve a encender.

### 2. Dejar los dos modelos residentes

El más caro y el menos obvio. Si el modelo de embeddings no está cargado,
LM Studio lo carga en cada consulta y **al hacerlo desaloja al de chat**:

| | 1er token |
|---|---|
| chat solo | 0,08 s |
| chat después de un embedding | 3,94 s |

El embedding tarda 400 ms; el swap cuesta 3,8 s. `scripts_lms_setup.sh` carga
ambos con TTL de 24 h, y `/health` avisa si alguno dejó de estar residente.

**El tamaño del contexto casi no influye** (3 productos → 4,6 s; 14 → 5,3 s,
antes del fix). Recortar el prompt no era la optimización.
