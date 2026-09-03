#!/usr/bin/env bash
# Corre la descripción por visión hasta terminar, pensado para dejarlo de noche.
#
# Tres cosas que una corrida de 15 horas necesita y el script de Python no
# puede resolver solo:
#
#   1. `caffeinate` — si la Mac se duerme, el trabajo se detiene y a la
#      mañana hay 3 horas hechas en vez de 15.
#   2. Reintento — LM Studio corta la conexión cada tantos miles de pedidos.
#      Como el script es reanudable, alcanza con volver a lanzarlo: retoma
#      por los que siguen sin description_ia.
#   3. Bitácora con hora — para poder leer a la mañana qué pasó y cuándo.
set -uo pipefail
cd "$(dirname "$0")"

LOG="data/describir-$(date +%Y%m%d-%H%M).log"
mkdir -p data
MAX_INTENTOS=20

{
  echo "=== arranque $(date '+%F %T') ==="
  uv run python - <<'PY'
import psycopg
with psycopg.connect("postgresql://fadia:fadia@localhost:5433/fadia") as c:
    n = c.execute("""SELECT count(*) FROM product
        WHERE (description IS NULL OR length(trim(description))<20)
          AND description_ia IS NULL AND jsonb_array_length(images)>0""").fetchone()[0]
    print(f"    pendientes al empezar: {n:,}")
PY

  for intento in $(seq 1 $MAX_INTENTOS); do
    echo "--- intento $intento · $(date '+%F %T') ---"
    # PYTHONUNBUFFERED: sin esto el progreso se queda en el buffer de
    # stdout al pasar por `tee` y la bitácora aparece vacía hasta el
    # final, que es justo cuando ya no sirve para nada.
    PYTHONUNBUFFERED=1 caffeinate -is uv run python scripts_describir.py --hilos 6
    salida=$?

    quedan=$(docker compose exec -T db psql -U fadia -d fadia -tAc \
      "SELECT count(*) FROM product
       WHERE (description IS NULL OR length(trim(description))<20)
         AND description_ia IS NULL AND jsonb_array_length(images)>0" 2>/dev/null | tr -d '[:space:]')

    echo "    salida=$salida · quedan=${quedan:-?}"
    [ "${quedan:-1}" = "0" ] && { echo "    no queda nada"; break; }
    # Si terminó sin error pero quedan filas, algo las está salteando:
    # reintentar en loop sería girar en falso.
    [ "$salida" = "0" ] && { echo "    terminó ok pero quedan ${quedan}: se corta para revisar"; break; }
    sleep 30
  done

  echo "=== re-embedding $(date '+%F %T') ==="
  PYTHONUNBUFFERED=1 caffeinate -is uv run python scripts_embed.py --solo-ia --batch 64

  echo "=== fin $(date '+%F %T') ==="
  docker compose exec -T db psql -U fadia -d fadia -c "
    SELECT count(*) FILTER (WHERE description_ia IS NOT NULL) AS descritos,
           count(*) FILTER (WHERE description_ia IS NOT NULL
                             AND embedding_ia_at = description_ia_at) AS re_embebidos,
           count(*) FILTER (WHERE (description IS NULL OR length(trim(description))<20)
                             AND description_ia IS NULL
                             AND jsonb_array_length(images)>0) AS sin_resolver
    FROM product;"
} 2>&1 | tee -a "$LOG"

echo "bitácora: $LOG"
