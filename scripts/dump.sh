#!/usr/bin/env bash
# Exporta el conjunto de demostración CON los embeddings ya calculados.
#
# El valor del dump es justamente ese: quien lo reciba no necesita
# scrapear (horas) ni volver a embeber (minutos y un modelo cargado).
# Levanta el stack, restaura, y la búsqueda semántica funciona.
set -euo pipefail
cd "$(dirname "$0")/.."

SALIDA="${1:-data/fadia-demo.sql.gz}"
TOPE_AVELLANEDA="${TOPE_AVELLANEDA:-2000}"
MARCAS="'sunnyclothing','eyelit','47street','carocuore','markova'"

echo "[1/3] armando el subconjunto…"
docker compose exec -T db psql -U fadia -d fadia -v ON_ERROR_STOP=1 <<SQL
DROP SCHEMA IF EXISTS demo CASCADE;
CREATE SCHEMA demo;

CREATE TABLE demo.store AS
  SELECT * FROM store WHERE slug IN ($MARCAS,'avellaneda');

CREATE TABLE demo.product AS
  SELECT * FROM product WHERE store_slug IN ($MARCAS)
  UNION ALL
  SELECT * FROM (SELECT * FROM product WHERE store_slug='avellaneda'
                 ORDER BY scraped_at DESC LIMIT $TOPE_AVELLANEDA) a;

CREATE TABLE demo.variant AS
  SELECT v.* FROM variant v JOIN demo.product p USING (product_uid);

CREATE TABLE demo.price_point AS
  SELECT pp.* FROM price_point pp JOIN demo.product p USING (product_uid);
SQL

echo "[2/3] exportando…"
mkdir -p "$(dirname "$SALIDA")"
docker compose exec -T db pg_dump -U fadia -d fadia \
  --data-only --no-owner --no-privileges \
  --table='demo.*' \
  | sed -e 's/^SET search_path.*/SET search_path = public;/' -e 's/demo\./public./g' \
  | gzip > "$SALIDA"

docker compose exec -T db psql -U fadia -d fadia -c "DROP SCHEMA demo CASCADE;" >/dev/null

echo "[3/3] listo"
uv run python scripts/inspect_dump.py "$SALIDA"
