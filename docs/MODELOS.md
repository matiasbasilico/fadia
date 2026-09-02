# Instalar y configurar los modelos

FadIA necesita **dos** modelos que corren en tu máquina, no en Docker:

| rol | qué hace | por qué |
|---|---|---|
| **chat** | redacta la respuesta | recibe las filas que ya eligió Postgres y escribe sobre ellas |
| **embeddings** | convierte texto en vectores de 768 dimensiones | es lo que hace que "algo para un casamiento" encuentre vestidos de fiesta |

Los dos hablan la **API de OpenAI**, así que sirve cualquier runtime que la
exponga. Acá están documentados los dos caminos: **LM Studio**, que es con el
que está probado, y **Ollama** como alternativa.

> **Por qué no corren en Docker.** Un contenedor Linux en Mac no ve la GPU.
> Los modelos corren en el host y los servicios les hablan por
> `host.docker.internal`.

---

## Camino A — LM Studio (el probado)

### 1. Instalar

Bajalo de **https://lmstudio.ai** y arrastralo a Aplicaciones. Abrilo una vez
para que registre el CLI (`lms`) en `~/.lmstudio/bin`.

Si querés el CLI en el PATH de forma permanente:

```bash
echo 'export PATH="$HOME/.lmstudio/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
lms --version
```

### 2. Descargar los dos modelos

Desde la lupa de la app, buscá y descargá:

```
google/gemma-4-e4b                        ← chat
text-embedding-nomic-embed-text-v1.5      ← embeddings
```

O desde la terminal:

```bash
lms get google/gemma-4-e4b
lms get text-embedding-nomic-embed-text-v1.5
```

### 3. Levantar el servidor y dejarlos residentes

```bash
make modelo
```

Eso corre `scripts_lms_setup.sh`, que hace tres cosas:

```bash
lms server start                              # puerto 1234
lms load google/gemma-4-e4b --ttl 86400 -y
lms load text-embedding-nomic-embed-text-v1.5 --ttl 86400 -y
```

> ### El `--ttl` no es un detalle
>
> Si los modelos no quedan residentes, LM Studio carga el de embeddings en
> cada consulta y **al hacerlo desaloja al de chat**. Medido en este
> proyecto: una llamada de embeddings de 400 ms terminaba costando **3,8 s**
> de recarga, y el primer token pasaba de 0,08 s a 4,4 s.
>
> Con los dos fijos por 24 h, el problema desaparece.

### 4. Verificar

```bash
lms ps                                    # los dos tienen que aparecer
curl -s http://localhost:1234/v1/models | python3 -m json.tool
```

Y desde la app:

```bash
curl -s http://localhost:8080/health | python3 -m json.tool
```

Tiene que decir `"chat_model_loaded": true` y `"embed_model_loaded": true`.

---

## Camino B — Ollama (alternativa)

> **Aviso honesto:** el proyecto está probado con LM Studio. Ollama expone la
> misma API de OpenAI y la configuración de abajo es la que corresponde, pero
> **no está verificada de punta a punta en este repo**. Si la usás y algo no
> encaja, empezá mirando `src/fadiaapi/llm.py`, que es el único archivo que
> habla con el runtime.

### 1. Instalar

```bash
brew install ollama
ollama serve          # o abrí la app; escucha en el puerto 11434
```

### 2. Bajar los equivalentes

```bash
ollama pull gemma3:4b          # chat
ollama pull nomic-embed-text   # embeddings, 768 dimensiones
```

**El modelo de embeddings tiene que dar 768 dimensiones.** La columna de la
base es `vector(768)` y el índice HNSW está construido sobre eso. Si usás otro,
hay que cambiar el esquema y **recalcular los 149.595 embeddings**.
`nomic-embed-text` da 768 y es el mismo que usa el camino A.

### 3. Apuntar la app a Ollama

Ollama sirve la API compatible en `/v1`. En `docker-compose.yml`, servicio
`api`:

```yaml
environment:
  LMSTUDIO_URL: http://host.docker.internal:11434/v1
  CHAT_MODEL: gemma3:4b
  EMBED_MODEL: nomic-embed-text
  REASONING_EFFORT: ""        # ver abajo
```

O sin tocar el archivo:

```bash
LMSTUDIO_URL=http://host.docker.internal:11434/v1 \
CHAT_MODEL=gemma3:4b \
EMBED_MODEL=nomic-embed-text \
REASONING_EFFORT= \
docker compose up -d --build api
```

> ### `REASONING_EFFORT` vacío en Ollama
>
> `reasoning_effort: "none"` es un parámetro que entiende LM Studio y que
> apaga la cadena de razonamiento de Gemma. En este proyecto bajó la respuesta
> de **29 s a 6 s**, y después a 1,4 s junto con los modelos residentes.
>
> Ollama puede rechazar el parámetro por desconocido. `llm.py` sólo lo manda
> si la variable no está vacía, así que dejala en blanco para Ollama. La
> contra es que Gemma vuelve a "pensar" antes de escribir y las respuestas
> tardan más.

### 4. Dejar los modelos residentes

El equivalente del `--ttl` de LM Studio:

```bash
export OLLAMA_KEEP_ALIVE=24h
```

Y para que pueda tener **los dos cargados a la vez** —que es todo el punto—:

```bash
export OLLAMA_MAX_LOADED_MODELS=2
```

Sin eso, Ollama descarga uno para cargar el otro y volvés al problema de los
3,8 s por consulta.

### 5. Verificar

```bash
ollama ps                                     # los dos, con el keep-alive
curl -s http://localhost:11434/v1/models | python3 -m json.tool
curl -s http://localhost:8080/health | python3 -m json.tool
```

---

## Todas las variables

Se leen en `src/fadiaapi/llm.py` y se pasan por `docker-compose.yml`.

| variable | por defecto | para qué |
|---|---|---|
| `LMSTUDIO_URL` | `http://localhost:1234/v1` | dónde escucha el runtime |
| `CHAT_MODEL` | `google/gemma-4-e4b` | el que redacta |
| `EMBED_MODEL` | `text-embedding-nomic-embed-text-v1.5` | el que vectoriza |
| `MAX_TOKENS` | `1600` | holgado a propósito, ver abajo |
| `REASONING_EFFORT` | `none` | apaga la cadena de razonamiento |

> **`MAX_TOKENS` holgado no es desprolijidad.** Gemma 4 es un modelo de
> razonamiento: en una prueba gastó **728 de 885 tokens pensando** antes de
> escribir una línea. Con `max_tokens=120` devolvió string vacío y 117 tokens
> de razonamiento. Si lo bajás, la respuesta sale en blanco.

---

## Cuando algo no anda

**El punto verde del encabezado está en rojo.**
El runtime no responde. `curl http://localhost:1234/v1/models` (o `:11434`).
Si contesta desde el host pero no desde el contenedor, el problema es
`host.docker.internal`: revisá que `extra_hosts` siga en `docker-compose.yml`.

**La primera pregunta tarda 4 segundos y las siguientes 1,4.**
Los modelos no quedaron residentes. `lms ps` / `ollama ps` y volvé al paso del
`--ttl` o `OLLAMA_KEEP_ALIVE`.

**El chat contesta vacío.**
`MAX_TOKENS` demasiado bajo: el modelo se gastó el presupuesto razonando.
Subilo a 1600 o apagá el razonamiento con `REASONING_EFFORT=none`.

**Busca pero no entiende bien.**
El header dice "texto" en vez de "vectorial": el modelo de embeddings no está
respondiendo y la búsqueda cayó a trigrama. La app sigue andando, pero sin
comprensión semántica.

**Cambiaste `EMBED_MODEL` y ahora no encuentra nada.**
Los embeddings guardados son de otro modelo. Hay que recalcularlos:

```bash
uv run python scripts_embed.py
```

Y si el modelo nuevo no da 768 dimensiones, además hay que cambiar el esquema.
