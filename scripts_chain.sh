#!/usr/bin/env bash
# Espera a que termine el crawl y encadena: validar -> ingerir -> embeber.
#
# El crawl es reanudable y puede cortarse solo, así que no basta con
# "esperar N minutos": se espera a que el proceso desaparezca y recién
# ahí se procesa lo que quedó en disco.
set -uo pipefail
cd "$(dirname "$0")"

LOG=data/chain.log
OUT=data/avellaneda/products.jsonl
BASE=data/baselines/avellaneda.json

say(){ printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

say "esperando a que termine el crawl…"
while pgrep -f scripts_ava_crawl >/dev/null 2>&1; do sleep 60; done
say "crawl terminado · $(wc -l < "$OUT") fichas en disco"

# El crawl deja pendientes las URLs que dieron timeout. Una segunda pasada
# las recupera; es barata porque saltea todo lo ya hecho por checkpoint.
say "segunda pasada para recuperar timeouts…"
uv run python scripts_ava_crawl.py --max-concurrency 12 --start-concurrency 6 >>"$LOG" 2>&1
say "segunda pasada lista · $(wc -l < "$OUT") fichas"

say "validando…"
uv run python scripts_validate.py "$OUT" --expected 139044 \
    --save-baseline "$BASE" >>"$LOG" 2>&1
VERDICT=$?
if [ $VERDICT -ne 0 ]; then
  say "ARNÉS BLOQUEÓ el lote. No se ingiere. Revisá $LOG"
  exit 1
fi
say "arnés OK"

say "ingiriendo a Postgres…"
uv run python scripts_ingest.py "$OUT" --store avellaneda >>"$LOG" 2>&1 \
  && say "ingesta OK" || { say "FALLÓ la ingesta"; exit 1; }

say "generando embeddings de lo nuevo…"
uv run python scripts_embed.py >>"$LOG" 2>&1 \
  && say "embeddings OK" || { say "FALLÓ el embedding"; exit 1; }

TOTAL=$(docker compose exec -T db psql -U fadia -d fadia -tAc \
  "SELECT count(*) || ' productos, ' || count(*) FILTER (WHERE embedding IS NOT NULL) || ' indexados' FROM product;" 2>/dev/null)
say "LISTO · $TOTAL · el chat ya busca sobre el catálogo completo"
