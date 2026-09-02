#!/usr/bin/env bash
# Carga el conjunto de demostración SCRAPEANDO desde cero.
#
# Alternativa a `make restore`: tarda ~15 min y necesita LM Studio andando
# para los embeddings, pero trae precios de hoy en vez de los del dump.
set -euo pipefail
cd "$(dirname "$0")/.."
TOPE="${TOPE:-250}"

echo "== 5 marcas, una por adapter =="
uv run python scripts_add_brand.py \
  sunnyclothing.ar eyelit.com.ar 47street.com.ar carocuore.com.ar markova.com \
  --limite "$TOPE"

echo
echo "== muestra del marketplace mayorista (modo Por mayor) =="
if [ -f data/avellaneda/inventory.csv ]; then
  uv run python scripts_add_brand.py www.avellanedaauntoque.com \
    --nombre "Avellaneda a un Toque" --limite 2000
else
  echo "  (sin inventario previo; se omite — ver README, sección Avellaneda)"
fi

echo
echo "== embeddings =="
uv run python scripts_embed.py

echo
echo "== completar género donde falte =="
uv run python scripts_genero.py

docker compose exec -T db psql -U fadia -d fadia -tAc \
  "SELECT '  listo: '||count(*)||' productos · '
       ||count(*) FILTER (WHERE embedding IS NOT NULL)||' indexados' FROM product;"
