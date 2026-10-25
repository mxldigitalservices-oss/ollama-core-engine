#!/bin/sh
set -e
: "${API_TOKEN:?Falta la variable API_TOKEN}"
export OLLAMA_HOST="127.0.0.1:11434"
export PORT="${PORT:-8080}"

# Modelo base de código (cualquiera de la librería de Ollama: qwen2.5-coder:14b, deepseek-coder-v2, etc.)
BASE_MODEL="${BASE_MODEL:-qwen2.5-coder:7b}"
case "$BASE_MODEL" in
  *[!A-Za-z0-9._:/-]*) echo "BASE_MODEL contiene caracteres no válidos"; exit 1 ;;
esac

ollama serve &
OLLAMA_PID=$!

caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &

# Modelos extra para el selector del dashboard (p. ej. EXTRA_MODELS="deepseek-coder-v2:16b,qwen2.5-coder:14b")
EXTRA_MODELS="${EXTRA_MODELS:-}"
case "$EXTRA_MODELS" in
  *[!A-Za-z0-9._:/,-]*) echo "EXTRA_MODELS contiene caracteres no válidos"; exit 1 ;;
esac

# Agente programador autovalidado + dashboard web (requiere GITHUB_TOKEN y GITHUB_REPO)
if [ "${AGENT_ENABLED:-1}" = "1" ] && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
  (cd /opt && while true; do python3 -m agent.server || true; sleep 5; done) &
  echo "Agente programador activo en /agent/* y dashboard en /ui (repo ${GITHUB_REPO})"
else
  echo "Agente programador desactivado (define GITHUB_TOKEN y GITHUB_REPO para activarlo)"
fi

until ollama list >/dev/null 2>&1; do sleep 1; done
ollama pull "$BASE_MODEL"
sed "s|^FROM .*|FROM ${BASE_MODEL}|" /config/Modelfile > /tmp/Modelfile
ollama create elite-architect -f /tmp/Modelfile
echo "Modelo elite-architect listo (base: ${BASE_MODEL})"

# Descarga en segundo plano de los modelos extra (elite-architect ya está disponible)
if [ -n "$EXTRA_MODELS" ]; then
  (
    for m in $(echo "$EXTRA_MODELS" | tr ',' ' '); do
      [ "$m" = "$BASE_MODEL" ] && continue
      echo "Descargando modelo extra: $m"
      ollama pull "$m" || echo "No se pudo descargar $m"
    done
    echo "Modelos extra procesados"
  ) &
fi

wait $OLLAMA_PID
