# FadIA

Buscador conversacional de moda argentina. Scrapea las vidrieras públicas de
marcas y mayoristas, normaliza todo a un esquema único y te deja preguntar en
castellano: *"un vestido negro para un casamiento, en verde, algo más largo"*.

**Postgres resuelve la búsqueda; el modelo solo redacta.** Gemma nunca elige
productos ni inventa precios: recibe las filas que ya eligió la base y escribe
sobre ellas. Si la recuperación falla, el modelo lo dice en vez de rellenar el
hueco.

```
149.595 productos · 46 marcas · 206.082 variantes · 247.366 puntos de precio
7 adapters · 53 tests · todo corre local
```

---

## Qué necesitás

| | | |
|---|---|---|
| **Docker Desktop** | obligatorio | levanta Postgres, la API y el front |
| **LM Studio** | obligatorio para el chat | corre en tu Mac, no en Docker: necesita la GPU |
| **uv** | solo para los tests y los scripts | `brew install uv` |

Sin LM Studio la app igual levanta y busca por texto; lo que no vas a tener es
la respuesta redactada ni la búsqueda semántica.

**Probado en**: MacBook Air M2, 16 GB. Los dos modelos entran cómodos.

---

## Paso a paso

### 1. Cloná y entrá

```bash
git clone git@github.com:matiasbasilico/fadia.git
cd fadia
```

### 2. Preparár LM Studio

Abrí LM Studio y descargá **dos** modelos desde su buscador:

- `google/gemma-4-e4b` — el que redacta las respuestas
- `text-embedding-nomic-embed-text-v1.5` — el que convierte texto en vectores

Después andá a **Developer → Start Server** (queda en el puerto `1234`).

Y dejá los dos **residentes en memoria**:

```bash
make modelo
```

> **Por qué importa.** Si no quedan residentes, cada búsqueda desaloja al modelo
> de chat para cargar el de embeddings y viceversa. Medido: una llamada de
> 400 ms terminaba costando 3,8 s de recarga. Con los dos fijos, el primer
> token sale en ~0,08 s.

### 3. Levantá el stack

```bash
make up
```

Deja andando:

| servicio | dónde | qué es |
|---|---|---|
| `web` | http://localhost:5173 | la interfaz |
| `api` | http://localhost:8080 | FastAPI: búsqueda y chat |
| `db` | localhost:5433 | Postgres 17 + pgvector + pg_trgm |

### 4. Cargá los datos

Dos caminos. **Elegí uno.**

**A) El dump de demostración** — un minuto, y ya trae los embeddings
calculados. No necesitás el modelo para esto.

```bash
make restore
```

**B) Scrapear desde cero** — cinco tiendas, unos 15 minutos. Necesita LM Studio
prendido para calcular los embeddings.

```bash
make seed
```

### 5. Listo

Abrí **http://localhost:5173** y preguntá.

```bash
make estado    # qué hay cargado ahora mismo
```

---

## Los comandos

```
make ayuda     muestra todos los targets
make up        levanta base + api + web
make down      detiene el stack (conserva los datos)
make logs      sigue los logs de la api
make modelo    deja los dos modelos residentes en LM Studio
make restore   carga el dump de demostración
make dump      exporta el conjunto actual a data/fadia-demo.sql.gz
make seed      scrapea las 5 tiendas desde cero
make reset     BORRA la base y la deja vacía
make test      corre los tests de regresión
make estado    qué hay cargado ahora mismo
```

> `make reset` corre `docker compose down -v`: **se lleva puestos los datos y
> los embeddings**. Ofrece hacer un dump antes. Si dudás, no lo corras.

---

## Cómo está armado

```
                    ┌─────────────── INGESTA ───────────────┐

  7 adapters  ─────►  crawler  ─────►   arnés   ─────►  Postgres 17
  uno por            adaptativo        53 tests         + pgvector
  plataforma         AIMD              bloquea el       + pg_trgm
                                       lote malo             │
                    └───────────────────────────────────────┘
                                                              │
                    ┌─────────────── CONSULTA ──────────────┐ │
                                                              ▼
  tu pregunta ─┬──► filtros por reglas ──► embedding ──► búsqueda
               │    (se acumulan del       768 dims       40 ms
   el hilo ────┘     hilo entero)                            │
                                                              ▼
                                            resultados ──► interfaz
                                                  │
                                                  ▼
                                            Gemma 4 redacta
                                            (no elige, no inventa)
```

La grilla llega a los 40 ms. El texto empieza a los 200 ms. Nunca mirás una
pantalla vacía.

### Los siete adapters

Uno por **plataforma**, no por tienda: el tema visual cambia en cada tienda,
el endpoint no.

| adapter | fuente de verdad | la trampa |
|---|---|---|
| `tiendanube` | `data-variants` | el JSON-LD publica el precio de lista, no el de venta |
| `avellaneda` | sitemap + JSON-LD | los listados por rubro se randomizan en cada request |
| `shopify` | `/products.json` | `compare_at_price` llega `"0.00"`, no `null` |
| `vtex` | `catalog_system` | el total va en un header; a veces la API solo vive en el dominio interno |
| `magento` | `/graphql` | `uid` no existe antes de 2.4.2 y tumba la query entera |
| `zara` | `categories?ajax` | la home no dice "zara" ni una vez: hay que detectar por host |
| `generico` | JSON-LD / microdata | solo 2 de 25 marcas sin adapter publican `Product` |

### Estructura

```
src/fadia/            el scraper
  adapters/           un archivo por plataforma
  normalize/          money, size, color, taxonomy, text
  storage/            esquema y repositorio
  models.py           esquema canónico (pydantic)
  validate.py         el arnés de validación
src/fadiaapi/         la API
  search.py           filtros, facetas y consultas
  main.py             endpoints y prompt del sistema
  llm.py              cliente de LM Studio
web/index.html        la interfaz entera, sin build
scripts/              dump, restore, reset, seed
tests/                los tests de regresión
```

---

## Por qué hay un arnés de validación

Ninguno de los peores incidentes tiró una excepción. Todos devolvieron
HTTP 200 con datos malos:

- Precios inflados un 54 % porque el JSON-LD publica el precio de lista.
- Talles de nena (4, 6, 8, 10) guardados como si fueran colores.
- 600 páginas descargadas con `Accept-Encoding: br` sin brotli instalado:
  200 en todas, cero productos, ningún error.
- Páginas 404 servidas con HTTP 200 y contenido de "no encontrado".

Por eso `make test` no es opcional: compara la **forma** del resultado contra
lo que ya se sabe del sitio y bloquea el lote antes de escribir.

---

## Cosas que conviene saber

**`hnsw.iterative_scan` no es opcional.** HNSW es un índice aproximado que
post-filtra: busca los vecinos y recién después aplica el `WHERE`. Con un
filtro selectivo devolvía **0 filas teniendo 1.938 coincidencias**, y la
relajación automática lo tapaba devolviendo maquillaje. Se corrige con
`SET LOCAL hnsw.iterative_scan = relaxed_order`.

**La contraseña de Postgres es `fadia:fadia`.** Es una credencial de desarrollo
y el contenedor sólo escucha en `localhost`. Si lo exponés a una red, cambiala.

**El vector se arma con el hilo, no con el último mensaje.** La ocasión
("para un casamiento") no es filtrable y vive sólo en el embedding: si el
vector saliera únicamente de la última pregunta, en el tercer turno "algo más
largo" devuelve abrigos.

**El vocabulario textil es argentino.** *morley* (4.903 prendas), *frisa*
(3.365), *bengalina* (1.216), gabardina, rústico. No existen en el inglés de
moda: ningún buscador extranjero los tiene.

---

## Reiniciar todo

```bash
make dump      # 1. guardá lo que hay  (opcional pero recomendado)
make reset     # 2. borra la base entera
make up        # 3. levantá de nuevo
make restore   # 4. cargá el dump
```

El dump viaja con la columna `embedding` ya calculada: quien lo reciba no
necesita el modelo ni volver a scrapear.

---

## Licencia y alcance

Proyecto personal. Los datos salen de vidrieras públicas y se usan para
responder consultas, no para entrenar modelos. `robots.txt` de cada sitio se
respeta, con 1 req/s de base y backoff adaptativo.

**FadIA no vende**: muestra lo que la tienda publica y te lleva hasta ahí.
