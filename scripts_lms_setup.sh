#!/usr/bin/env bash
# Deja LM Studio listo para que el chat responda rápido.
#
# Los dos modelos tienen que quedar RESIDENTES. Si el de embeddings no lo
# está, LM Studio lo carga en cada consulta y al hacerlo desaloja al de
# chat: medido, eso agrega 3,8 s por pregunta (1er token de 0,08 s a 4,4 s).
set -euo pipefail
export PATH="$HOME/.lmstudio/bin:$PATH"

CHAT="${CHAT_MODEL:-google/gemma-4-e4b}"
EMBED="${EMBED_MODEL:-text-embedding-nomic-embed-text-v1.5}"

lms server start >/dev/null
lms load "$CHAT"  --ttl 86400 -y >/dev/null 2>&1 || true
lms load "$EMBED" --ttl 86400 -y >/dev/null 2>&1 || true

echo "modelos residentes:"
lms ps 2>/dev/null | tail -n +2
