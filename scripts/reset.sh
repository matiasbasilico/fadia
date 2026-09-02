#!/usr/bin/env bash
# Borra TODO y deja la base vacía con el esquema recién creado.
#
# Pide confirmación porque el volumen de Postgres se destruye: los
# embeddings y el histórico de precios no se recuperan salvo que exista
# un dump. Por eso el primer paso es ofrecer uno.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Esto ELIMINA la base de datos completa (productos, embeddings, histórico)."
docker compose exec -T db psql -U fadia -d fadia -tAc \
  "SELECT '  hoy contiene: '||count(*)||' productos, '||count(DISTINCT store_slug)||' tiendas'
   FROM product;" 2>/dev/null || echo "  (la base no está levantada)"

if [ "${1:-}" != "--si" ]; then
  read -r -p "¿Guardar un dump antes de borrar? [S/n] " r
  [[ "${r:-S}" =~ ^[SsYy]?$ ]] && ./scripts/dump.sh "data/backup-$(date +%Y%m%d-%H%M).sql.gz"
  read -r -p "Escribí BORRAR para confirmar: " c
  [ "$c" = "BORRAR" ] || { echo "cancelado"; exit 1; }
fi

echo "[1/3] bajando el stack y su volumen…"
docker compose down -v

echo "[2/3] levantando la base limpia (aplica schema.sql sola)…"
docker compose up -d db
for i in $(seq 1 40); do
  docker compose exec -T db pg_isready -U fadia -d fadia >/dev/null 2>&1 && break
  sleep 2
done

echo "[3/3] verificando…"
docker compose exec -T db psql -U fadia -d fadia -tAc \
  "SELECT '  tablas: '||count(*) FROM pg_tables WHERE schemaname='public';"
docker compose exec -T db psql -U fadia -d fadia -tAc \
  "SELECT '  extensiones: '||string_agg(extname,', ') FROM pg_extension WHERE extname<>'plpgsql';"
echo
echo "Base vacía. Ahora:"
echo "  make restore   → carga el dump de demostración (rápido, con embeddings)"
echo "  make seed      → scrapea las 5 tiendas desde cero (lento, necesita el modelo)"
