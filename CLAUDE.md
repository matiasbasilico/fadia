# CLAUDE.md

Contexto para trabajar en este repo. Lo que sigue es lo que no se deduce
leyendo el código: por qué las cosas están como están, y qué ya se rompió.

---

## Qué es

Buscador conversacional de moda argentina. Scrapea vidrieras públicas,
normaliza a un esquema único y responde preguntas en castellano.

**La decisión de arquitectura central:** Postgres resuelve la búsqueda, el
modelo solo redacta. Gemma recibe las filas que Postgres ya eligió, como
contexto de solo lectura. No puede elegir productos ni inventar precios.

En una prueba la recuperación falló y le llegaron cosméticos cuando el usuario
pedía remeras. Contestó: *"No hay productos de indumentaria en el contexto que
me pasaste."* Si el modelo hubiera podido elegir, habría rellenado el hueco con
productos plausibles e inexistentes. **No muevas esa arista.**

---

## Comandos

```bash
make up        # levanta db + api + web
make test      # 53 tests de regresión — corrélos siempre
make estado    # qué hay cargado
make logs      # logs de la api
```

Los tests corren con `uv run pytest -q` **desde la raíz del repo**. Si el
directorio de trabajo cambió, `uv run` falla con "Failed to spawn: pytest".

### El contenedor de la API hornea el código

`docker-compose.yml` sólo montea `./data` en `api`. El código Python está
dentro de la imagen, así que **`docker compose restart api` corre la versión
vieja**. Después de tocar `src/`:

```bash
docker compose up -d --build api
```

`web/` sí está montado: los cambios en `web/index.html` se ven recargando.

---

## Estructura

```
src/fadia/            scraper
  adapters/           un archivo por PLATAFORMA (no por tienda)
  normalize/          money, size, color, taxonomy, text
  validate.py         arnés de validación
src/fadiaapi/
  search.py           Filters, parse_filters, facetas, texto_de_busqueda
  main.py             endpoints + SYSTEM prompt
  llm.py              cliente LM Studio
web/index.html        la interfaz entera, sin build ni dependencias
```

`web/index.html` es un solo archivo con CSS y JS embebidos. Es deliberado: no
hay bundler, no hay `node_modules`, se edita y se recarga.

---

## Idioma y tono

- **Todo el código, los comentarios y los commits van en castellano rioplatense.**
- Los comentarios explican **por qué**, no qué. Si un comentario se puede
  deducir leyendo la línea de abajo, sobra.
- El tono del modelo es **editorial, no coloquial**. Está en el `SYSTEM` de
  `main.py` y prohíbe explícitamente "che", "te tiro", "copado", "onda". Se
  pidió expresamente sacar el registro rioplatense de las respuestas al
  usuario. El código sí es rioplatense; las respuestas del modelo no.

---

## Trampas que ya costaron caro

### `[hidden]` pierde contra cualquier `display` de autor

`[hidden]{display:none}` es regla del **user-agent**. Cualquier
`.foo{display:flex}` la anula. Pasó dos veces: el modal cerrado se comía todos
los toques en mobile, y "empezar de cero" aparecía en la pantalla de inicio.

Hay un `[hidden]{display:none !important}` global en `web/index.html`. **No lo
saques.**

### `overflow:hidden` recorta las sombras hacia afuera

`.card` tiene `overflow:hidden`. Cualquier `box-shadow` sin `inset` sobre un
hijo queda comido. El anillo de la tarjeta citada usa `inset` por esto.

### `hnsw.iterative_scan` no es opcional

HNSW post-filtra: busca los vecinos aproximados y recién después aplica el
`WHERE`. Con un filtro selectivo devolvía **0 filas de 1.938 coincidencias**,
en silencio. Hay un `SET LOCAL hnsw.iterative_scan = relaxed_order` en las
consultas. Si desaparece, la búsqueda filtrada devuelve vacío sin avisar.

### La normalización se hornea al scrapear

`classify_category`, `normalize_size` y compañía se llaman **sólo en los
adapters**. El JSONL guarda el resultado ya cocinado y el ingest lo copia sin
recalcular. Consecuencia: arreglar el normalizador **no toca lo ya guardado**.

Dos deudas abiertas por esto:
- **3.514 zapatillas clasificadas como `pants`** (de 5.361). El clasificador
  actual devuelve `shoes` correctamente; las filas son viejas.
- **2.772 variantes con talles padeados** (`S00`, `M00`, `XL0`) en 219
  productos, porque el normalizador no reconoce ese formato de Eyelit.

Un pase de reclasificación offline las arregla: el JSONL conserva `title` y
`category_path`, que es todo lo que el clasificador necesita.

### Magento devuelve un placeholder con HTTP 200

`/media/catalog/product/cache/<hash>/…` sirve el logo de Magento (262×262) si
esa variante de redimensionado nunca se generó. **No da 404.** Las Pepas tiene
el catálogo entero así.

El arreglo está en el front y es reactivo: si la imagen que llegó tiene la
firma del placeholder, se reemplaza por el original sin el segmento `cache/`.
No se le saca el caché a todos porque complot y bowen responden bien y sus
originales pesan mucho más.

### Los CDN aceptan que les pidas el tamaño

Medido en frío: VTEX 2891 ms, Shopify 804 ms, Avellaneda 249 ms, bajando fotos
de 1200×1600 para tarjetas de 160 px.

- **VTEX**: `/arquivos/ids/<id>-<ancho>-auto/` → funciona
- **Shopify**: `?width=<n>` → funciona
- **Tiendanube**: sólo sirve tamaños ya generados, cualquier otro da 404
- **Avellaneda**: ignora el parámetro

Está en `imgChica()`. **Comparar el host parseado, no la URL entera**: un
`(^|\.)cdn\.shopify\.com` sobre el texto completo nunca coincide, porque antes
del host va `//` y no un punto. Ese bug estuvo activo y no se notaba.

---

## Medir en el navegador con Orca

Se usa `orca` (CLI) para probar la interfaz. Dos cosas que confunden y hacen
perder tiempo:

**1. Las transiciones se congelan si la pestaña no tiene foco.**
`getComputedStyle` devuelve el valor viejo y parece un bug de cascada. Pasó
tres veces. Para descartarlo:

```js
el.style.transition='none'; void el.offsetHeight;
getComputedStyle(el).color        // ahora sí es el valor final
```

**2. `orca eval` va a la pestaña activa.** Si el usuario cambia de pestaña,
tus comandos van a la de él. Creá una propia y direccionala:

```bash
orca tab create --url "http://localhost:5173"    # devuelve un page id
orca eval --page <id> --expression "..."
```

También: el emulador de dispositivo **sólo redimensiona**, no simula
`hover:none` ni `pointer:coarse`. Las reglas `@media (hover:none)` no se pueden
verificar ahí.

---

## El hilo de conversación

Dos mecanismos distintos, no los confundas:

1. **Filtros duros** (`parse_filters`): categoría, color, talle, tela, detalle,
   precio. Se acumulan a lo largo del hilo; lo nuevo pisa a lo viejo.
2. **El vector** (`texto_de_busqueda`): se arma con los últimos 3 turnos más el
   mensaje actual, repitiendo el actual para que pese más que **cualquier turno
   previo individual**.

La comparación es contra el turno más largo, no contra la suma: exigir que gane
a todo el hilo junto obligaría a tirar los turnos viejos, que es justo lo que
se quiere conservar.

Sin esto, la **ocasión** se pierde. "Casamiento" no es filtrable: vive sólo en
el embedding. Con el vector armado únicamente con el último mensaje, al tercer
turno "algo más largo" devolvía abrigos.

---

## Datos que no están estructurados

**No hay envíos ni devoluciones.** Ninguna de las 7 plataformas los publica
estructurados. Los 27.817 productos que mencionan "envío" lo tienen como texto
libre en la descripción (`"HACEMOS ENVIOS Y PUNTOS DE ENCUENTRO"`).

La solapa del modal se llama "Envíos y compra" y muestra procedencia real:
local, dirección, compra mínima, la frase textual del vendedor si existe, y
cuándo se verificó el precio. **No inventes una política de devoluciones.**

**El histórico de precios casi no se mueve.** 743 productos de 149.595 tienen
un cambio real. Un sparkline sería plano en el 99,5 % de los casos. Se muestra
como badge puntual sólo cuando hay una baja ≥ 5 %.

---

## Antes de dar algo por hecho

- Corré `make test`. 53 tests.
- Si tocaste `src/`, reconstruí la imagen de la api.
- Verificá en el navegador, no por lectura del código. La mitad de los bugs de
  esta sesión (el modal comiéndose los toques, el placeholder de Magento, los
  controles que se plegaban antes del clic) eran invisibles en el código y
  obvios midiendo.
- Cuando algo parezca un bug de CSS, descartá primero la transición congelada.
