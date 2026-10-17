#!/bin/sh
set -e
: "${API_TOKEN:?Falta la variable API_TOKEN}"
export OLLAMA_HOST="127.0.0.1:11434"
export PORT="${PORT:-8080}"

ollama serve &
OLLAMA_PID=$!

caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &

until ollama list >/dev/null 2>&1; do sleep 1; done
ollama pull llama3.2
ollama create elite-architect -f /config/Modelfile
echo "Modelo elite-architect listo"

wait $OLLAMA_PID
