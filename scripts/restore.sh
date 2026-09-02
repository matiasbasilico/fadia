#!/usr/bin/env bash
# Restaura el dump de demostración.
#
# El dump trae la columna `embedding` ya calculada: quien lo reciba no
# necesita el modelo ni volver a scrapear. Levantar el stack y correr esto
# alcanza para que la búsqueda semántica funcione.
#
# Dos cosas que hicieron falta y no son obvias:
#
#   1. `pg_dump --data-only` escribe las tablas en orden ALFABÉTICO, así
#      que `product` entra antes que `store` y la clave foránea lo rechaza.
#      `session_replication_role = replica` suspende los triggers de
#      integridad durante la carga; se restablecen al cerrar la sesión.
#
#   2. La carga va en UNA transacción. Sin eso, un error a mitad de camino
#      deja tablas a medio llenar y el reintento choca con claves
#      duplicadas: es exactamente lo que pasó la primera vez que se probó.
set -euo pipefail
cd "$(dirname "$0")/.."

ENTRADA="${1:-data/fadia-demo.sql.gz}"

if [ ! -f "$ENTRADA" ]; then
  echo "  no existe $ENTRADA"
  echo "  generalo con:  make dump   (desde una base ya cargada)"
  exit 1
fi

echo "[1/3] esperando a la base ..."
for _ in $(seq 1 40); do
  if docker compose exec -T db pg_isready -U fadia -d fadia >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# Restaurar sobre tablas con datos choca por clave duplicada. Si hay algo,
# se avisa y se corta: vaciar la base es una decisión del usuario
# (`make reset`), no un efecto colateral de restaurar.
HAY=$(docker compose exec -T db psql -U fadia -d fadia -tAc \
  "SELECT count(*) FROM product" 2>/dev/null | tr -d "[:space:]" || echo 0)
if [ "${HAY:-0}" != "0" ]; then
  echo "  la base ya tiene $HAY productos."
  echo "  vaciala primero con:  make reset"
  exit 1
fi

echo "[2/3] restaurando ${ENTRADA} ..."
{
  echo "SET session_replication_role = replica;"
  echo "BEGIN;"
  gunzip -c "$ENTRADA"
  echo "COMMIT;"
} | docker compose exec -T db psql -U fadia -d fadia -q -v ON_ERROR_STOP=1

# Un dump con IDs explícitos NO mueve las secuencias de identidad: el
# próximo INSERT arranca de 1 y choca con `variant_pkey`. Hay que
# adelantarlas al máximo cargado o la primera cosecha posterior falla.
echo "[3/4] adelantando las secuencias de identidad ..."
docker compose exec -T db psql -U fadia -d fadia -q -c \
  "SELECT setval(pg_get_serial_sequence('variant','id'),
                 COALESCE((SELECT max(id) FROM variant), 1), true);
   SELECT setval(pg_get_serial_sequence('crawl_run','id'),
                 COALESCE((SELECT max(id) FROM crawl_run), 1), true);"

echo "[4/4] sellando la fecha de corrida por tienda ..."
docker compose exec -T db psql -U fadia -d fadia -q -c \
  "UPDATE store s SET last_crawl = sub.m
     FROM (SELECT store_slug, max(scraped_at) AS m FROM product GROUP BY 1) sub
    WHERE sub.store_slug = s.slug;"

docker compose exec -T db psql -U fadia -d fadia -tAc \
  "SELECT '  ' || count(*) || ' productos - '
       || count(*) FILTER (WHERE embedding IS NOT NULL) || ' con embedding - '
       || count(DISTINCT store_slug) || ' tiendas' FROM product;"
